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

-- Legacy groups retain one canonical MD5 identity; phase-1 scenes enumerate
-- every exact source and delivered object below. Use MD5 for ALL media,
-- including generated assets, because Drive supplies MD5 natively;
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
  primary key (tenant_id, group_key, fingerprint),
  foreign key (tenant_id, group_key) references public.visual_group(gym_id, group_key)
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
-- Other sources require explicit selected-byte evidence with an actor and
-- source; the writer bundle records bytes read from the delivered object.
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

-- Writer preparation is one transaction: a failed alias, group or identity
-- check rolls back the entire bundle. Tenant lock order matches the existing
-- alias RPC, including when two writers first see no registered aliases.
-- Registration is immutable but does not reserve or mark bytes used; only the
-- subsequent calendar trigger may create global usage. A failed calendar
-- write can safely retry this exact bundle and fingerprint.
create or replace function public.visual_global_prepare_bundle(
  p_tenant text, p_aliases jsonb, p_fingerprint text,
  p_evidence jsonb, p_actor text, p_asset_id text default null
) returns jsonb language plpgsql security definer set search_path = public as $$
declare v_tenant text; v_item jsonb; v_kind text; v_value text;
  v_group text; v_found text; v_registered text; v_identity text;
  v_seen jsonb:='[]'::jsonb;
begin
  v_tenant := public.visual_group_tenant_strict(p_tenant)::text;
  if p_tenant is distinct from v_tenant then
    raise exception 'canonical visual tenant required' using errcode='22023';
  end if;
  p_fingerprint := lower(btrim(p_fingerprint));
  if p_fingerprint is null or p_fingerprint !~ '^md5:[0-9a-f]{32}$'
      or p_evidence is null or jsonb_typeof(p_evidence)<>'object'
      or p_evidence->>'verified_bytes' is distinct from p_fingerprint
      or nullif(btrim(p_actor),'') is null
      or p_aliases is null or jsonb_typeof(p_aliases)<>'array' then
    raise exception 'invalid exact-byte preparation bundle' using errcode='22023';
  end if;
  if jsonb_array_length(p_aliases)<2 or jsonb_array_length(p_aliases)>16 then
    raise exception 'invalid exact-byte preparation bundle' using errcode='22023';
  end if;
  perform public.visual_group_auxiliary_lock(hashtextextended(
    jsonb_build_array('visual_tenant',v_tenant)::text,0));
  perform public.visual_group_auxiliary_lock(hashtextextended(
    jsonb_build_array('visual_global_fingerprint',p_fingerprint)::text,0));
  -- Validate the full bundle before making any writes. Duplicate aliases are
  -- rejected so the caller cannot silently omit part of its evidence.
  for v_item in select value from jsonb_array_elements(p_aliases) loop
    v_kind:=v_item->>'alias_kind'; v_value:=v_item->>'alias_value';
    if jsonb_typeof(v_item)<>'object'
        or v_kind is null or v_value is null
        or v_item <> jsonb_build_object('alias_kind',v_kind,'alias_value',v_value)
        or v_kind not in ('source_asset','drive_id','byte_hash','canonical_url','r2_key')
        or nullif(btrim(v_value),'') is null
        or (v_kind='byte_hash' and lower(btrim(v_value)) !~
          '^(source|derived):(sha256:[0-9a-f]{64}|md5:[0-9a-f]{32})$')
        or (v_kind='byte_hash' and lower(btrim(v_value)) not in
          ('source:'||p_fingerprint,'derived:'||p_fingerprint))
        or v_seen @> jsonb_build_array(v_item) then
      raise exception 'invalid or duplicate visual alias' using errcode='22023';
    end if;
    v_seen:=v_seen||jsonb_build_array(v_item);
  end loop;
  if not p_aliases @> jsonb_build_array(jsonb_build_object(
      'alias_kind','byte_hash','alias_value','derived:'||p_fingerprint))
      or (select count(*) from jsonb_array_elements(p_aliases) a
        where a.value->>'alias_kind'='canonical_url')<>1
      or not p_aliases @> jsonb_build_array(jsonb_build_object(
        'alias_kind','canonical_url','alias_value',p_evidence->>'delivered_url'))
      or (nullif(btrim(p_asset_id),'') is not null and not p_aliases @>
        jsonb_build_array(jsonb_build_object('alias_kind','source_asset',
          'alias_value',btrim(p_asset_id)))) then
    raise exception 'bundle lacks selected byte, URL or asset alias' using errcode='23514';
  end if;
  -- All existing aliases must already agree; no first-writer choice may
  -- reassign a conflicting alias to a freshly minted group.
  for v_item in select value from jsonb_array_elements(p_aliases) loop
    select a.group_key into v_found from public.visual_group_alias a
      where a.gym_id=v_tenant and a.alias_kind=v_item->>'alias_kind'
        and a.alias_value=case when v_item->>'alias_kind'='byte_hash'
          then lower(btrim(v_item->>'alias_value')) else v_item->>'alias_value' end;
    if v_found is not null then
      if v_group is not null and v_group<>v_found then
        raise exception 'bundle aliases belong to conflicting groups' using errcode='23514';
      end if;
      v_group:=v_found;
    end if;
  end loop;
  -- The current global claim can verify only one delivered URL per group.
  -- Refuse a second URL before any alias is written; otherwise a successful
  -- preparation RPC could make an already reserved group unclaimable.
  if v_group is not null and exists(
      select 1 from public.visual_group_alias a
      where a.gym_id=v_tenant and a.group_key=v_group
        and a.alias_kind='canonical_url'
        and not p_aliases @> jsonb_build_array(jsonb_build_object(
          'alias_kind','canonical_url','alias_value',a.alias_value))) then
    raise exception 'group has another delivered URL without per-URL byte evidence'
      using errcode='23514';
  end if;
  for v_item in select value from jsonb_array_elements(p_aliases) loop
    v_registered:=public.visual_group_register_alias(v_tenant,
      v_item->>'alias_kind',v_item->>'alias_value',v_group);
    if v_group is not null and v_registered<>v_group then
      raise exception 'alias registration returned conflicting group' using errcode='23514';
    end if;
    v_group:=v_registered;
  end loop;
  v_identity:=public.visual_global_register_identity(v_tenant,v_group,
    p_fingerprint,p_evidence,p_actor,p_asset_id);
  if v_identity<>p_fingerprint then
    raise exception 'identity registration returned conflicting fingerprint' using errcode='23514';
  end if;
  return jsonb_build_object('group_key',v_group,'fingerprint',v_identity);
end;
$$;

-- Phase 1 source/rendition preparation. These receipts are inserted only by
-- the database owner after an actual exact-object read or render operation.
-- The exposed service-role RPC may consume them, never manufacture them.
-- Before activation, preparation does not claim, release, or activate usage.
-- After the fleet authority is active, expanding an already occupied scene
-- refreshes its complete fingerprint set atomically on the original date.
create table if not exists public.visual_global_object_read_receipt (
  receipt_id uuid primary key default gen_random_uuid(),
  tenant_id text not null,
  exact_url text not null check (exact_url = btrim(exact_url) and exact_url ~ '^https?://[^[:space:]]+$'),
  fingerprint text not null check (fingerprint ~ '^md5:[0-9a-f]{32}$'),
  byte_length bigint not null check (byte_length > 0),
  acquisition_method text not null check (acquisition_method in ('verified_object_read','drive_asset')),
  asset_id text,
  evidence_ref text not null check (btrim(evidence_ref) <> ''),
  observed_by text not null check (btrim(observed_by) <> ''),
  observed_at timestamptz not null default now(),
  unique (receipt_id,tenant_id,exact_url,fingerprint),
  check ((acquisition_method = 'drive_asset') = (asset_id is not null))
);
create table if not exists public.visual_global_render_receipt (
  receipt_id uuid primary key default gen_random_uuid(),
  tenant_id text not null,
  source_read_receipt uuid not null references public.visual_global_object_read_receipt(receipt_id),
  delivered_read_receipt uuid not null references public.visual_global_object_read_receipt(receipt_id),
  source_exact_url text not null,
  delivered_exact_url text not null,
  source_fingerprint text not null check (source_fingerprint ~ '^md5:[0-9a-f]{32}$'),
  delivered_fingerprint text not null check (delivered_fingerprint ~ '^md5:[0-9a-f]{32}$'),
  operation text not null check (operation in ('render','reburn','rehost')),
  evidence_ref text not null check (btrim(evidence_ref) <> ''),
  rendered_by text not null check (btrim(rendered_by) <> ''),
  rendered_at timestamptz not null default now(),
  check (source_exact_url <> delivered_exact_url),
  foreign key (source_read_receipt,tenant_id,source_exact_url,source_fingerprint)
    references public.visual_global_object_read_receipt(receipt_id,tenant_id,exact_url,fingerprint),
  foreign key (delivered_read_receipt,tenant_id,delivered_exact_url,delivered_fingerprint)
    references public.visual_global_object_read_receipt(receipt_id,tenant_id,exact_url,fingerprint)
);
create table if not exists public.visual_global_object_attestation (
  exact_url text primary key,
  tenant_id text not null,
  group_key text not null,
  fingerprint text not null check (fingerprint ~ '^md5:[0-9a-f]{32}$'),
  byte_length bigint not null check (byte_length > 0),
  acquisition_method text not null,
  evidence_ref text not null check (btrim(evidence_ref) <> ''),
  read_receipt uuid not null references public.visual_global_object_read_receipt(receipt_id),
  attested_by text not null check (btrim(attested_by) <> ''),
  attested_at timestamptz not null default now(),
  unique (tenant_id, exact_url),
  unique (tenant_id, group_key, exact_url, fingerprint),
  foreign key (read_receipt,tenant_id,exact_url,fingerprint)
    references public.visual_global_object_read_receipt(receipt_id,tenant_id,exact_url,fingerprint),
  foreign key (tenant_id, group_key) references public.visual_group(gym_id,group_key)
);
create table if not exists public.visual_global_scene_object_member (
  tenant_id text not null,
  group_key text not null,
  exact_url text not null,
  fingerprint text not null check (fingerprint ~ '^md5:[0-9a-f]{32}$'),
  object_role text not null check (object_role in ('source','delivered')),
  recorded_at timestamptz not null default now(),
  primary key (tenant_id, exact_url, object_role),
  foreign key (tenant_id, group_key) references public.visual_group(gym_id,group_key),
  foreign key (tenant_id,group_key,exact_url,fingerprint)
    references public.visual_global_object_attestation(tenant_id,group_key,exact_url,fingerprint)
);
create index if not exists visual_global_scene_object_member_group_idx
  on public.visual_global_scene_object_member(tenant_id,group_key,fingerprint);
create table if not exists public.visual_global_object_lineage (
  tenant_id text not null,
  group_key text not null,
  source_exact_url text not null,
  delivered_exact_url text not null,
  source_fingerprint text not null check (source_fingerprint ~ '^md5:[0-9a-f]{32}$'),
  delivered_fingerprint text not null check (delivered_fingerprint ~ '^md5:[0-9a-f]{32}$'),
  render_receipt uuid not null unique references public.visual_global_render_receipt(receipt_id),
  recorded_at timestamptz not null default now(),
  primary key (tenant_id,source_exact_url,delivered_exact_url),
  foreign key (tenant_id,group_key) references public.visual_group(gym_id,group_key),
  foreign key (tenant_id,group_key,source_exact_url,source_fingerprint)
    references public.visual_global_object_attestation(tenant_id,group_key,exact_url,fingerprint),
  foreign key (tenant_id,group_key,delivered_exact_url,delivered_fingerprint)
    references public.visual_global_object_attestation(tenant_id,group_key,exact_url,fingerprint)
);
create index if not exists visual_global_object_lineage_source_idx
  on public.visual_global_object_lineage(source_exact_url,source_fingerprint);

alter table public.visual_global_object_read_receipt enable row level security;
alter table public.visual_global_render_receipt enable row level security;
alter table public.visual_global_object_attestation enable row level security;
alter table public.visual_global_scene_object_member enable row level security;
alter table public.visual_global_object_lineage enable row level security;
revoke all on public.visual_global_object_read_receipt, public.visual_global_render_receipt,
  public.visual_global_object_attestation, public.visual_global_scene_object_member,
  public.visual_global_object_lineage from public,anon,authenticated,service_role;
grant select on public.visual_global_object_attestation, public.visual_global_scene_object_member,
  public.visual_global_object_lineage to service_role;
create policy visual_global_object_attestation_read on public.visual_global_object_attestation
  for select to service_role using (true);
create policy visual_global_scene_object_member_read on public.visual_global_scene_object_member
  for select to service_role using (true);
create policy visual_global_object_lineage_read on public.visual_global_object_lineage
  for select to service_role using (true);
create trigger visual_global_read_receipt_immutable before update or delete
  on public.visual_global_object_read_receipt for each row execute function public.visual_global_immutable();
create trigger visual_global_render_receipt_immutable before update or delete
  on public.visual_global_render_receipt for each row execute function public.visual_global_immutable();
create trigger visual_global_object_attestation_immutable before update or delete
  on public.visual_global_object_attestation for each row execute function public.visual_global_immutable();
create trigger visual_global_scene_object_member_immutable before update or delete
  on public.visual_global_scene_object_member for each row execute function public.visual_global_immutable();
create trigger visual_global_object_lineage_immutable before update or delete
  on public.visual_global_object_lineage for each row execute function public.visual_global_immutable();

-- The legacy alias RPC has only tenant-local locks. Serialize exact URLs
-- against phase-1 preparation and reject later contradictory aliases.
create or replace function public.visual_global_exact_url_alias_guard()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if new.alias_kind='canonical_url' then
    if current_setting('transaction_isolation')<>'read committed' then
      raise exception 'exact visual URL registration requires READ COMMITTED isolation'
        using errcode='25001';
    end if;
    if not pg_try_advisory_xact_lock(hashtextextended(
        jsonb_build_array('visual_exact_url',new.alias_value)::text,0)) then
      raise exception 'exact visual URL busy; retry transaction' using errcode='55P03';
    end if;
    if exists(select 1 from public.visual_global_object_attestation o
        where o.exact_url=new.alias_value
          and (o.tenant_id<>new.gym_id or o.group_key<>new.group_key)) then
      raise exception 'exact URL is already attested to another tenant or scene'
        using errcode='23514';
    end if;
  end if;
  return new;
end;
$$;
create trigger visual_global_exact_url_alias_guard before insert
  on public.visual_group_alias for each row execute function public.visual_global_exact_url_alias_guard();

create or replace function public.visual_global_prepare_source_rendition(
  p_tenant text, p_group_key text, p_source_read_receipt uuid,
  p_delivered_read_receipt uuid, p_render_receipt uuid, p_actor text
) returns jsonb language plpgsql security definer set search_path = public as $$
declare v_tenant text; v_source public.visual_global_object_read_receipt%rowtype;
  v_delivered public.visual_global_object_read_receipt%rowtype;
  v_render public.visual_global_render_receipt%rowtype;
  v_item public.visual_global_object_read_receipt%rowtype;
  v_object public.visual_global_object_attestation%rowtype;
  v_role text; v_group text; v_asset_hash text; v_fingerprint text; v_refreshed integer:=0;
begin
  v_tenant := public.visual_group_tenant_strict(p_tenant)::text;
  if current_setting('transaction_isolation')<>'read committed' then
    raise exception 'exact visual preparation requires READ COMMITTED isolation'
      using errcode='25001';
  end if;
  if p_tenant is distinct from v_tenant or nullif(btrim(p_group_key),'') is null
      or nullif(btrim(p_actor),'') is null then
    raise exception 'canonical tenant, scene and actor required' using errcode='22023';
  end if;
  perform public.visual_group_auxiliary_lock(hashtextextended(
    jsonb_build_array('visual_tenant',v_tenant)::text,0));
  if not exists(select 1 from public.visual_group
      where gym_id=v_tenant and group_key=p_group_key) then
    raise exception 'scene does not belong to canonical tenant' using errcode='23514';
  end if;
  -- Lock the complete component in the same deterministic order used by the
  -- calendar guard and manual-link RPC. Preparation may expand an already
  -- occupied linked scene, so locking only the requested endpoint can invert
  -- the runtime group-lock order.
  perform public.visual_group_lock_scene_components(jsonb_build_array(
    jsonb_build_object('gym_id',v_tenant,'group_key',p_group_key)));
  select * into v_source from public.visual_global_object_read_receipt
    where receipt_id=p_source_read_receipt;
  select * into v_delivered from public.visual_global_object_read_receipt
    where receipt_id=p_delivered_read_receipt;
  if v_source.receipt_id is null or v_delivered.receipt_id is null
      or v_source.tenant_id<>v_tenant or v_delivered.tenant_id<>v_tenant then
    raise exception 'source and delivered read receipts must belong to canonical tenant'
      using errcode='23514';
  end if;
  for v_item in select distinct on (exact_url) * from public.visual_global_object_read_receipt
      where receipt_id in (p_source_read_receipt,p_delivered_read_receipt)
      order by exact_url,receipt_id loop
    if not pg_try_advisory_xact_lock(hashtextextended(
        jsonb_build_array('visual_exact_url',v_item.exact_url)::text,0)) then
      raise exception 'exact visual URL busy; retry transaction' using errcode='55P03';
    end if;
  end loop;
  if v_source.exact_url<>v_delivered.exact_url then
    select * into v_render from public.visual_global_render_receipt
      where receipt_id=p_render_receipt;
    if v_render.receipt_id is null or v_render.tenant_id<>v_tenant
        or v_render.source_read_receipt<>v_source.receipt_id
        or v_render.delivered_read_receipt<>v_delivered.receipt_id
        or v_render.source_exact_url<>v_source.exact_url
        or v_render.delivered_exact_url<>v_delivered.exact_url
        or v_render.source_fingerprint<>v_source.fingerprint
        or v_render.delivered_fingerprint<>v_delivered.fingerprint then
      raise exception 'actual render receipt does not bind exact input and output bytes'
        using errcode='23514';
    end if;
  elsif p_render_receipt is not null or v_source.fingerprint<>v_delivered.fingerprint
      or v_source.byte_length<>v_delivered.byte_length then
    raise exception 'same exact URL cannot have conflicting bytes or render receipt'
      using errcode='23514';
  end if;
  -- Validate both trusted reads and existing URL bindings before inserting
  -- either object. Shared exact-URL locks and the alias trigger close races
  -- with later alias registration; the unique key closes attestation races.
  for v_item in select * from public.visual_global_object_read_receipt
      where receipt_id in (p_source_read_receipt,p_delivered_read_receipt)
      order by exact_url,receipt_id loop
    if v_item.acquisition_method='drive_asset' then
      if not exists(select 1 from public.visual_group_alias a
          where a.gym_id=v_tenant and a.group_key=p_group_key
            and a.alias_kind='source_asset' and a.alias_value=v_item.asset_id) then
        raise exception 'Drive asset is not a member of the visual scene'
          using errcode='23514';
      end if;
      select lower(btrim(a.content_hash)) into v_asset_hash from public.media_asset a
        where a.id=v_item.asset_id
          and public.visual_group_tenant_id(a.gym_id)::text=v_tenant;
      if v_asset_hash is null or v_asset_hash !~ '^[0-9a-f]{32}$'
          or v_item.fingerprint<>'md5:'||v_asset_hash then
        raise exception 'Drive asset tenant or MD5 does not match exact read receipt'
          using errcode='23514';
      end if;
    end if;
    select a.group_key into v_group from public.visual_group_alias a
      where a.gym_id=v_tenant and a.alias_kind='canonical_url'
        and a.alias_value=v_item.exact_url;
    if exists(select 1 from public.visual_group_alias a
        where a.gym_id<>v_tenant and a.alias_kind='canonical_url'
          and a.alias_value=v_item.exact_url) then
      raise exception 'exact URL is already aliased to a foreign tenant' using errcode='23514';
    end if;
    if v_group is not null and v_group<>p_group_key then
      raise exception 'exact URL is already bound to another scene' using errcode='23514';
    end if;
    select * into v_object from public.visual_global_object_attestation
      where exact_url=v_item.exact_url;
    if v_object.exact_url is not null and
        (v_object.tenant_id<>v_tenant or v_object.group_key<>p_group_key
         or v_object.fingerprint<>v_item.fingerprint
         or v_object.byte_length<>v_item.byte_length) then
      raise exception 'exact URL is already bound to another tenant, scene or byte object'
        using errcode='23514';
    end if;
  end loop;
  -- Bind every exact object URL to the scene in the same transaction as its
  -- attestation. Sorted registration keeps concurrent source/rendition
  -- preparations on a deterministic lock path. A later claim can therefore
  -- prove that no canonical URL in the scene was omitted from the byte set.
  for v_item in select distinct on (exact_url) *
      from public.visual_global_object_read_receipt
      where receipt_id in (p_source_read_receipt,p_delivered_read_receipt)
      order by exact_url,receipt_id loop
    v_group:=public.visual_group_register_alias(
      v_tenant,'canonical_url',v_item.exact_url,p_group_key);
    if v_group<>p_group_key then
      raise exception 'exact URL registration returned another scene' using errcode='23514';
    end if;
  end loop;
  -- Python returns the selected delivered MD5 as a derived byte_hash. The
  -- calendar resolver requires that exact hint to be registered as well as
  -- both URLs. Keep each attested object's alias in the same scene/transaction;
  -- a previously bound conflicting scene aborts all new evidence registration.
  for v_fingerprint in select distinct fingerprint
      from public.visual_global_object_read_receipt
      where receipt_id in (p_source_read_receipt,p_delivered_read_receipt)
      order by fingerprint loop
    v_group:=public.visual_group_register_alias(
      v_tenant,'byte_hash','derived:'||v_fingerprint,p_group_key);
    if v_group<>p_group_key then
      raise exception 'exact byte hash registration returned another scene' using errcode='23514';
    end if;
  end loop;
  for v_role in select unnest(array['source','delivered']) loop
    if v_role='source' then v_item:=v_source; else v_item:=v_delivered; end if;
    insert into public.visual_global_object_attestation
      (exact_url,tenant_id,group_key,fingerprint,byte_length,acquisition_method,evidence_ref,read_receipt,attested_by)
      values (v_item.exact_url,v_tenant,p_group_key,v_item.fingerprint,v_item.byte_length,
              v_item.acquisition_method,v_item.evidence_ref,v_item.receipt_id,btrim(p_actor))
      on conflict (exact_url) do nothing;
    select * into v_object from public.visual_global_object_attestation
      where exact_url=v_item.exact_url;
    if v_object.tenant_id<>v_tenant or v_object.group_key<>p_group_key
        or v_object.fingerprint<>v_item.fingerprint or v_object.byte_length<>v_item.byte_length then
      raise exception 'exact URL was rebound concurrently' using errcode='23514';
    end if;
    insert into public.visual_global_scene_object_member
      (tenant_id,group_key,exact_url,fingerprint,object_role)
      values(v_tenant,p_group_key,v_item.exact_url,v_item.fingerprint,v_role)
      on conflict do nothing;
  end loop;
  if v_source.exact_url<>v_delivered.exact_url then
    insert into public.visual_global_object_lineage
      (tenant_id,group_key,source_exact_url,delivered_exact_url,
       source_fingerprint,delivered_fingerprint,render_receipt)
      values(v_tenant,p_group_key,v_source.exact_url,v_delivered.exact_url,
             v_source.fingerprint,v_delivered.fingerprint,v_render.receipt_id)
      on conflict (tenant_id,source_exact_url,delivered_exact_url) do nothing;
    if not exists(select 1 from public.visual_global_object_lineage l
        where l.tenant_id=v_tenant and l.group_key=p_group_key
          and l.source_exact_url=v_source.exact_url
          and l.delivered_exact_url=v_delivered.exact_url
          and l.source_fingerprint=v_source.fingerprint
          and l.delivered_fingerprint=v_delivered.fingerprint
          and l.render_receipt=v_render.receipt_id) then
      raise exception 'lineage already bound to a different render receipt' using errcode='23514';
    end if;
  end if;
  -- Once the global authority is active, adding exact source/rendition bytes
  -- to an already occupied scene must inherit that scene's original usage in
  -- this same transaction. This is a history refresh, not a new usage decision:
  -- the writer contract keeps usage_claimed=false and the calendar trigger
  -- remains the only path that initially consumes an unused scene.
  v_refreshed:=public.visual_global_refresh_scene_history(v_tenant,p_group_key);
  return jsonb_build_object('group_key',p_group_key,'source_fingerprint',v_source.fingerprint,
    'delivered_fingerprint',v_delivered.fingerprint,
    'usage_claimed',false,'history_refreshed',v_refreshed>0,
    'refreshed_local_groups',v_refreshed);
end;
$$;

-- A scene can contain several exact byte objects: the source, its delivered
-- rendition, and objects in manually linked component groups. Enumerate every
-- verified MD5 object. The integrated authority never falls back to the legacy
-- single identity: every component must have complete phase-1 object evidence.
create or replace function public.visual_global_scene_fingerprints(
  p_tenant text,p_group text
) returns table(fingerprint text)
language sql stable security definer set search_path = public as $$
  select distinct m.fingerprint
  from public.visual_group_scene_members(p_tenant,p_group) sm(group_key)
  join public.visual_global_scene_object_member m
    on m.tenant_id=p_tenant and m.group_key=sm.group_key
  where m.fingerprint ~ '^md5:[0-9a-f]{32}$';
$$;

create or replace function public.visual_global_group_bytes_verified(
  p_tenant text,p_group text,p_fingerprint text
) returns boolean language sql stable security definer set search_path = public as $$
  select p_fingerprint ~ '^md5:[0-9a-f]{32}$'
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

create or replace function public.visual_global_scene_complete(
  p_tenant text,p_group text
) returns boolean language plpgsql stable security definer set search_path = public as $$
declare v_component text;
begin
  if nullif(btrim(p_tenant),'') is null or nullif(btrim(p_group),'') is null
      or not exists(select 1 from public.visual_group
        where gym_id=p_tenant and group_key=p_group) then
    return false;
  end if;
  for v_component in select sm.group_key
      from public.visual_group_scene_members(p_tenant,p_group) sm(group_key) loop
    -- Each component must explicitly bind at least one exact source and one
    -- delivered object. Even equal source/delivery bytes retain both roles.
    if not exists(select 1 from public.visual_global_scene_object_member m
        where m.tenant_id=p_tenant and m.group_key=v_component
          and m.object_role='source')
       or not exists(select 1 from public.visual_global_scene_object_member m
        where m.tenant_id=p_tenant and m.group_key=v_component
          and m.object_role='delivered') then
      return false;
    end if;
    if exists(select 1 from public.visual_group_alias a
        where a.gym_id=p_tenant and a.group_key=v_component
          and a.alias_kind='canonical_url'
          and not exists(select 1
            from public.visual_global_object_attestation o
            join public.visual_global_scene_object_member m
              on m.tenant_id=o.tenant_id and m.group_key=o.group_key
               and m.exact_url=o.exact_url and m.fingerprint=o.fingerprint
            where o.tenant_id=p_tenant and o.group_key=v_component
              and o.exact_url=a.alias_value))
       or exists(select 1 from public.visual_global_scene_object_member m
          left join public.visual_global_object_attestation o
            on o.tenant_id=m.tenant_id and o.group_key=m.group_key
             and o.exact_url=m.exact_url and o.fingerprint=m.fingerprint
          where m.tenant_id=p_tenant and m.group_key=v_component
            and o.exact_url is null) then
      return false;
    end if;
    -- Every distinct source/delivery role must be connected by immutable
    -- render lineage. Same-URL source/delivery pairs need no render receipt.
    if exists(select 1 from public.visual_global_scene_object_member m
        where m.tenant_id=p_tenant and m.group_key=v_component
          and m.object_role='source'
          and not exists(select 1 from public.visual_global_scene_object_member d
            where d.tenant_id=m.tenant_id and d.group_key=m.group_key
              and d.object_role='delivered' and d.exact_url=m.exact_url
              and d.fingerprint=m.fingerprint)
          and not exists(select 1 from public.visual_global_object_lineage l
            join public.visual_global_scene_object_member d
              on d.tenant_id=l.tenant_id and d.group_key=l.group_key
               and d.exact_url=l.delivered_exact_url
               and d.fingerprint=l.delivered_fingerprint
               and d.object_role='delivered'
            where l.tenant_id=m.tenant_id and l.group_key=m.group_key
              and l.source_exact_url=m.exact_url
              and l.source_fingerprint=m.fingerprint))
       or exists(select 1 from public.visual_global_scene_object_member m
        where m.tenant_id=p_tenant and m.group_key=v_component
          and m.object_role='delivered'
          and not exists(select 1 from public.visual_global_scene_object_member s
            where s.tenant_id=m.tenant_id and s.group_key=m.group_key
              and s.object_role='source' and s.exact_url=m.exact_url
              and s.fingerprint=m.fingerprint)
          and not exists(select 1 from public.visual_global_object_lineage l
            join public.visual_global_scene_object_member s
              on s.tenant_id=l.tenant_id and s.group_key=l.group_key
               and s.exact_url=l.source_exact_url
               and s.fingerprint=l.source_fingerprint
               and s.object_role='source'
            where l.tenant_id=m.tenant_id and l.group_key=m.group_key
              and l.delivered_exact_url=m.exact_url
              and l.delivered_fingerprint=m.fingerprint)) then
      return false;
    end if;
  end loop;
  return exists(select 1
    from public.visual_global_scene_fingerprints(p_tenant,p_group));
end;
$$;

-- Legacy row fingerprint helper remains for pre-activation repair reports and
-- compatibility RPCs. The integrated trigger/activation authority below never
-- accepts it in place of exact source/delivered object evidence.
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
  if v_row_hash is null and (v_asset_hash is null or
      nullif(btrim(to_jsonb(p_row)->>'source_media_url'),'') is distinct from
      nullif(btrim(p_row.image_url),'')) then
    return null;
  end if;
  return coalesce(v_row_hash,v_asset_hash);
end;
$$;

-- Follow the actual render chain (A -> B -> C), never synthesize an A -> C
-- receipt. Exact URL AND fingerprint continuity is required at every hop.
-- Bound scene size, traversal rows and depth; corruption, cycles and excess
-- traversal refuse the row rather than returning a partial proof.
create or replace function public.visual_global_lineage_verified(
  p_tenant text,p_group text,p_source_url text,p_source_hash text,
  p_delivered_url text,p_delivered_hash text
) returns boolean language plpgsql stable security definer set search_path = public as $$
declare v_groups text[]; v_edges bigint; v_found boolean; v_invalid boolean; v_walks bigint;
begin
  if p_tenant is null or public.visual_group_tenant_id(p_tenant)::text is distinct from p_tenant
      or p_group is null or p_source_url is null or p_delivered_url is null
      or p_source_hash is null or p_delivered_hash is null then return false; end if;
  select array_agg(sm.group_key) into v_groups
    from (select group_key from public.visual_group_scene_members(p_tenant,p_group) members(group_key)
      limit 1025) sm;
  if coalesce(cardinality(v_groups),0)=0 or cardinality(v_groups)>1024 then return false; end if;
  -- Inspect every edge originating in this scene. A foreign or inconsistent
  -- edge cannot be silently filtered out and leave a seemingly valid proof.
  select count(*) into v_edges from (
    select 1 from public.visual_global_object_lineage l
    where exists(select 1 from public.visual_global_scene_object_member m
      where m.tenant_id=p_tenant and m.group_key=any(v_groups)
        and m.exact_url=l.source_exact_url) limit 4097) limited;
  if v_edges=0 or v_edges>4096 then return false; end if;
  if exists(select 1 from public.visual_global_object_lineage l
    left join public.visual_global_render_receipt r
      on r.receipt_id=l.render_receipt and r.tenant_id=l.tenant_id
        and r.source_exact_url=l.source_exact_url and r.delivered_exact_url=l.delivered_exact_url
        and r.source_fingerprint=l.source_fingerprint and r.delivered_fingerprint=l.delivered_fingerprint
    left join public.visual_global_object_attestation s
      on s.tenant_id=l.tenant_id and s.group_key=l.group_key
        and s.exact_url=l.source_exact_url and s.fingerprint=l.source_fingerprint
    left join public.visual_global_object_attestation d
      on d.tenant_id=l.tenant_id and d.group_key=l.group_key
        and d.exact_url=l.delivered_exact_url and d.fingerprint=l.delivered_fingerprint
    left join public.visual_global_object_read_receipt sr
      on sr.receipt_id=r.source_read_receipt and sr.tenant_id=l.tenant_id
        and sr.exact_url=l.source_exact_url and sr.fingerprint=l.source_fingerprint
        and sr.byte_length=s.byte_length
    left join public.visual_global_object_read_receipt dr
      on dr.receipt_id=r.delivered_read_receipt and dr.tenant_id=l.tenant_id
        and dr.exact_url=l.delivered_exact_url and dr.fingerprint=l.delivered_fingerprint
        and dr.byte_length=d.byte_length
    where exists(select 1 from public.visual_global_scene_object_member m
      where m.tenant_id=p_tenant and m.group_key=any(v_groups)
        and m.exact_url=l.source_exact_url)
      and (l.tenant_id<>p_tenant or not l.group_key=any(v_groups)
        or r.receipt_id is null or s.exact_url is null or d.exact_url is null
        or sr.receipt_id is null or dr.receipt_id is null
        or not exists(select 1 from public.visual_global_scene_object_member m
          where m.tenant_id=p_tenant and m.group_key=l.group_key
            and m.exact_url=l.source_exact_url and m.fingerprint=l.source_fingerprint
            and m.object_role='source')
        or not exists(select 1 from public.visual_global_scene_object_member m
          where m.tenant_id=p_tenant and m.group_key=l.group_key
            and m.exact_url=l.delivered_exact_url and m.fingerprint=l.delivered_fingerprint
            and m.object_role='delivered'))) then return false; end if;
  with recursive edges as materialized (
    select l.* from public.visual_global_object_lineage l
      where l.tenant_id=p_tenant and l.group_key=any(v_groups)
        and exists(select 1 from public.visual_global_scene_object_member m
          where m.tenant_id=p_tenant and m.group_key=l.group_key
            and m.exact_url=l.source_exact_url and m.fingerprint=l.source_fingerprint)
  ), walk(exact_url,fingerprint,depth,path,cycle) as (
    select p_source_url,p_source_hash,0,array[p_source_url],false
    union all
    select e.delivered_exact_url,e.delivered_fingerprint,w.depth+1,
      w.path||e.delivered_exact_url,e.delivered_exact_url=any(w.path)
    from walk w join edges e
      on e.source_exact_url=w.exact_url and e.source_fingerprint=w.fingerprint
    where w.depth<32 and not w.cycle
  )
  select coalesce(bool_or(w.exact_url=p_delivered_url and w.fingerprint=p_delivered_hash
      and not w.cycle),false),
    coalesce(bool_or(w.cycle or (w.depth=32 and exists(select 1 from edges e
      where e.source_exact_url=w.exact_url and e.source_fingerprint=w.fingerprint))),false),
    count(*) into v_found,v_invalid,v_walks
    from (select * from walk limit 4097) w;
  return v_found and not v_invalid and v_walks<=4096;
end;
$$;

create or replace function public.visual_global_row_bytes_verified(
  p_row public.content_calendar
) returns boolean language plpgsql stable security definer set search_path = public as $$
declare v_tenant text; v_group text; v_source_url text; v_delivered_url text;
  v_source_hash text; v_delivered_hash text; v_selected text; v_kind text;
  v_thumbnail_url text;
begin
  v_tenant:=public.visual_group_tenant_id(p_row.gym_id)::text;
  v_group:=p_row.visual_group_key;
  v_delivered_url:=nullif(btrim(p_row.image_url),'');
  v_thumbnail_url:=nullif(btrim(to_jsonb(p_row)->>'thumbnail_url'),'');
  -- Interim poster hold: the phase-1 scene ledger attests image/source objects,
  -- not a third poster object. Blank is safe and an exact URL match reuses the
  -- already verified delivered object. Preserve and reject every distinct
  -- poster so direct database writers cannot invent poster lineage.
  if v_thumbnail_url is not null and
      (to_jsonb(p_row)->>'thumbnail_url') is distinct from
      (to_jsonb(p_row)->>'image_url') then
    return false;
  end if;
  -- An absent source is unknown, even when the delivered object has a valid
  -- receipt. Only the row can select which attested source fed its rendition.
  v_source_url:=nullif(btrim(to_jsonb(p_row)->>'source_media_url'),'');
  if v_tenant is null or v_group is null or v_source_url is null
      or v_delivered_url is null
      or not public.visual_global_scene_complete(v_tenant,v_group) then
    return false;
  end if;
  select m.fingerprint into v_delivered_hash
    from public.visual_global_scene_object_member m
    join public.visual_group_scene_members(v_tenant,v_group) sm(group_key)
      on sm.group_key=m.group_key
    where m.tenant_id=v_tenant and m.exact_url=v_delivered_url
      and m.object_role='delivered';
  select m.fingerprint into v_source_hash
    from public.visual_global_scene_object_member m
    join public.visual_group_scene_members(v_tenant,v_group) sm(group_key)
      on sm.group_key=m.group_key
    where m.tenant_id=v_tenant and m.exact_url=v_source_url
      and m.object_role='source';
  if v_delivered_hash is null or v_source_hash is null then return false; end if;
  if v_source_url=v_delivered_url then
    -- A same-object selection must resolve to identical bytes in both roles.
    if v_source_hash<>v_delivered_hash then return false; end if;
  elsif not public.visual_global_lineage_verified(
      v_tenant,v_group,v_source_url,v_source_hash,v_delivered_url,v_delivered_hash) then
    return false;
  end if;
  if nullif(btrim(to_jsonb(p_row)->>'byte_hash'),'') is not null then
    if lower(btrim(to_jsonb(p_row)->>'byte_hash')) !~
        '^(source|derived):md5:[0-9a-f]{32}$' then return false; end if;
    v_kind:=split_part(lower(btrim(to_jsonb(p_row)->>'byte_hash')),':',1);
    v_selected:=split_part(lower(btrim(to_jsonb(p_row)->>'byte_hash')),':',2)||':'||
      split_part(lower(btrim(to_jsonb(p_row)->>'byte_hash')),':',3);
    if (v_kind='source' and v_selected<>v_source_hash)
        or (v_kind='derived' and v_selected<>v_delivered_hash) then return false; end if;
  end if;
  return true;
end;
$$;

-- All fingerprints are locked in sorted order and preflighted before any
-- usage/member write. A collision on any source, rendition, or linked
-- component aborts the statement and rolls back the whole calendar mutation.
create or replace function public.visual_global_claim_fingerprint_set(
  p_tenant text,p_group_key text,p_date date,p_row_id uuid,p_channel text,
  p_published boolean,p_ambiguous boolean,p_fingerprints text[]
) returns text[] language plpgsql security definer set search_path = public as $$
declare v_fingerprints text[]; v_hash text;
  v_usage public.visual_global_usage%rowtype;
  v_member public.visual_global_usage_member%rowtype;
begin
  p_tenant:=public.visual_group_tenant_strict(p_tenant)::text;
  if p_published is null or p_ambiguous is null or (p_date is null and not p_published)
      or not exists(select 1 from public.visual_group
        where gym_id=p_tenant and group_key=p_group_key) then
    raise exception 'claim needs a canonical scene and verified date or permanent publication'
      using errcode='22023';
  end if;
  select array_agg(f order by f) into v_fingerprints from (
    select distinct lower(btrim(x)) f from unnest(p_fingerprints) x
  ) q where f ~ '^md5:[0-9a-f]{32}$';
  if v_fingerprints is null
      or cardinality(v_fingerprints)<>cardinality(p_fingerprints)
      or exists(select 1 from unnest(p_fingerprints) x
        where x is null or lower(btrim(x)) !~ '^md5:[0-9a-f]{32}$') then
    raise exception 'complete unique MD5 fingerprint set required' using errcode='22023';
  end if;
  foreach v_hash in array v_fingerprints loop
    perform public.visual_group_auxiliary_lock(hashtextextended(
      jsonb_build_array('visual_global_fingerprint',v_hash)::text,0));
  end loop;
  -- Preflight existing owners and members before inserting any absent key.
  for v_usage in select u.* from public.visual_global_usage u
      where u.fingerprint=any(v_fingerprints) order by u.fingerprint for update loop
    if v_usage.state='released' then
      raise exception 'legacy released global fingerprint requires historical repair'
        using errcode='23514';
    elsif v_usage.tenant_id<>p_tenant or v_usage.used_date is distinct from p_date then
      raise exception 'visual byte fingerprint already used by another client or date'
        using errcode='23514';
    end if;
  end loop;
  for v_member in select m.* from public.visual_global_usage_member m
      where m.tenant_id=p_tenant and m.group_key=p_group_key
        and m.fingerprint=any(v_fingerprints)
      order by m.fingerprint for update loop
    if v_member.state='released' then
      raise exception 'legacy released global member requires historical repair'
        using errcode='23514';
    elsif v_member.used_date is distinct from p_date then
      raise exception 'visual group has conflicting historical membership'
        using errcode='23514';
    end if;
  end loop;
  if exists(select 1 from public.visual_global_usage_member m
      where m.tenant_id=p_tenant and m.group_key=p_group_key
        and not (m.fingerprint=any(v_fingerprints))) then
    raise exception 'visual group has an unenumerated historical fingerprint'
      using errcode='23514';
  end if;
  foreach v_hash in array v_fingerprints loop
    insert into public.visual_global_usage
      (fingerprint,tenant_id,used_date,state,ambiguous,published_at)
      values(v_hash,p_tenant,p_date,
        case when p_published then 'published' else 'reserved' end,p_ambiguous,
        case when p_published then now() end)
      on conflict (fingerprint) do nothing;
    select * into v_usage from public.visual_global_usage
      where fingerprint=v_hash for update;
    if v_usage.state='released' or v_usage.tenant_id<>p_tenant
        or v_usage.used_date is distinct from p_date then
      raise exception 'visual byte fingerprint owner changed during atomic claim'
        using errcode='23514';
    end if;
    if p_published and v_usage.state='reserved' then
      update public.visual_global_usage set state='published',published_at=now(),
        ambiguous=ambiguous or p_ambiguous where fingerprint=v_hash;
    elsif p_ambiguous and not v_usage.ambiguous and v_usage.state='reserved' then
      update public.visual_global_usage set ambiguous=true where fingerprint=v_hash;
    end if;
    insert into public.visual_global_usage_member
      (tenant_id,group_key,fingerprint,calendar_row_id,channel,used_date,state,ambiguous)
      values(p_tenant,p_group_key,v_hash,p_row_id,p_channel,p_date,
        case when p_published then 'published' else 'reserved' end,p_ambiguous)
      on conflict (tenant_id,group_key,fingerprint) do update set
        state=case when excluded.state='published' then 'published'
          else public.visual_global_usage_member.state end,
        ambiguous=public.visual_global_usage_member.ambiguous or excluded.ambiguous
      -- Published members are immutable. PostgreSQL still fires BEFORE UPDATE
      -- triggers for a no-op DO UPDATE, so make repeat publish/sibling claims a
      -- real conflict no-op instead of touching an identical permanent row.
      where public.visual_global_usage_member.state<>'published'
        and (excluded.state='published' or
          (excluded.ambiguous and not public.visual_global_usage_member.ambiguous));
  end loop;
  return v_fingerprints;
end;
$$;

-- Scene identity can expand after the first fleet activation through either
-- exact source/rendition preparation or a human-confirmed component link.
-- Refresh every occupied local group in the connected component while the
-- caller still owns its ordered component locks. A collision on any newly
-- introduced byte aborts the identity mutation and all usage/member writes.
create or replace function public.visual_global_refresh_scene_history(
  p_tenant text,p_group_key text
) returns integer language plpgsql security definer set search_path = public as $$
declare r record; v_fingerprints text[]; v_count integer:=0;
begin
  p_tenant:=public.visual_group_tenant_strict(p_tenant)::text;
  if nullif(btrim(p_group_key),'') is null
      or not exists(select 1 from public.visual_group
        where gym_id=p_tenant and group_key=p_group_key) then
    raise exception 'scene history refresh requires a canonical scene'
      using errcode='22023';
  end if;
  -- Before the first activation, the fleet-wide importer owns historical
  -- consumption. Afterwards all identity expansion is immediately global,
  -- including for tenants that have not yet been individually armed.
  if not exists(select 1 from public.gym_visual_guard_settings where enforce) then
    return 0;
  end if;
  if not exists(select 1 from public.visual_group_usage_ledger l
      where l.gym_id=p_tenant and l.group_key in (
        select public.visual_group_scene_members(p_tenant,p_group_key))) then
    return 0;
  end if;
  if not public.visual_global_scene_complete(p_tenant,p_group_key) then
    raise exception 'occupied scene expansion has incomplete exact-byte evidence'
      using errcode='23514';
  end if;
  select array_agg(fingerprint order by fingerprint) into v_fingerprints
    from public.visual_global_scene_fingerprints(p_tenant,p_group_key);
  for r in select l.* from public.visual_group_usage_ledger l
      where l.gym_id=p_tenant and l.group_key in (
        select public.visual_group_scene_members(p_tenant,p_group_key))
      order by (l.state='published') desc,l.group_key loop
    perform public.visual_global_claim_fingerprint_set(
      r.gym_id,r.group_key,r.reserved_date,r.calendar_row_id,r.channel,
      r.state='published',r.ambiguous,v_fingerprints);
    v_count:=v_count+1;
  end loop;
  return v_count;
end;
$$;

create or replace function public.visual_global_scene_link_claim_guard()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  perform public.visual_global_refresh_scene_history(new.gym_id,new.group_key_a);
  return new;
end;
$$;
drop trigger if exists visual_global_scene_link_claim_guard
  on public.visual_group_scene_link;
create trigger visual_global_scene_link_claim_guard after insert
  on public.visual_group_scene_link for each row
  execute function public.visual_global_scene_link_claim_guard();

create or replace function public.visual_global_claim_scene(
  p_row public.content_calendar,p_published boolean,p_ambiguous boolean
) returns text[] language plpgsql security definer set search_path = public as $$
declare v_tenant text; v_fingerprints text[];
begin
  v_tenant:=public.visual_group_tenant_strict(p_row.gym_id)::text;
  if p_row.visual_group_key is null
      or not public.visual_global_row_bytes_verified(p_row) then
    raise exception 'selected calendar source and rendition bytes are not fully attested'
      using errcode='23514';
  end if;
  select array_agg(fingerprint order by fingerprint) into v_fingerprints
    from public.visual_global_scene_fingerprints(v_tenant,p_row.visual_group_key);
  return public.visual_global_claim_fingerprint_set(v_tenant,p_row.visual_group_key,
    p_row.post_date,p_row.id,p_row.account,p_published,p_ambiguous,v_fingerprints);
end;
$$;

-- Compatibility for pre-activation callers. Integrated calendar writes use
-- visual_global_claim_scene and cannot reduce a scene to one selected digest.
create or replace function public.visual_global_claim(
  p_gym_key text,p_group_key text,p_date date,p_row_id uuid,p_channel text,
  p_published boolean,p_ambiguous boolean default false,
  p_selected_fingerprint text default null
) returns text language plpgsql security definer set search_path = public as $$
declare v_tenant text; v_hash text;
begin
  v_tenant:=public.visual_group_tenant_strict(p_gym_key)::text;
  select fingerprint into v_hash from public.visual_global_identity
    where tenant_id=v_tenant and group_key=p_group_key;
  if v_hash is null or not public.visual_global_group_bytes_verified(
      v_tenant,p_group_key,v_hash)
      or p_selected_fingerprint is distinct from v_hash then
    raise exception 'legacy claim requires the exact verified group fingerprint'
      using errcode='23514';
  end if;
  perform public.visual_global_claim_fingerprint_set(v_tenant,p_group_key,p_date,
    p_row_id,p_channel,p_published,p_ambiguous,array[v_hash]);
  return v_hash;
end;
$$;

-- Release verifies every occupied byte but never frees it. Denied, swapped and
-- otherwise released staged photos remain globally consumed on their first date.
create or replace function public.visual_global_release(
  p_gym_key text,p_group_key text
) returns boolean language plpgsql security definer set search_path = public as $$
declare v_tenant text; v_hash text; v_usage public.visual_global_usage%rowtype;
  v_member public.visual_global_usage_member%rowtype;
begin
  v_tenant:=public.visual_group_tenant_strict(p_gym_key)::text;
  if not public.visual_global_scene_complete(v_tenant,p_group_key) then
    raise exception 'global scene byte evidence incomplete' using errcode='23514';
  end if;
  for v_hash in select fingerprint
      from public.visual_global_scene_fingerprints(v_tenant,p_group_key)
      order by fingerprint loop
    select * into v_usage from public.visual_global_usage
      where fingerprint=v_hash for update;
    select * into v_member from public.visual_global_usage_member
      where tenant_id=v_tenant and group_key=p_group_key
        and fingerprint=v_hash for update;
    if not found or v_usage.fingerprint is null or v_member.state='released'
        or v_usage.state='released' then
      raise exception 'global staged history is missing or legacy released'
        using errcode='23514';
    end if;
    if v_member.state='published' or v_member.ambiguous or v_usage.ambiguous
        or v_usage.tenant_id<>v_tenant
        or v_usage.used_date is distinct from v_member.used_date then
      raise exception 'global reservation still occupied or uncertain' using errcode='23514';
    end if;
  end loop;
  if not exists(select 1 from public.visual_group_usage_ledger l
      where l.gym_id=v_tenant and l.group_key=p_group_key
        and l.state='released' and not l.ambiguous)
      or exists(select 1 from public.visual_group_usage_sibling s
        where s.gym_id=v_tenant and s.group_key=p_group_key and s.state='active') then
    raise exception 'global reservation still occupied or uncertain' using errcode='23514';
  end if;
  return true;
end;
$$;

-- Fleet-wide import includes published, reserved, and released local history.
-- Released local rows are imported as reserved because staging consumes bytes.
create or replace function public.visual_global_import_history()
returns jsonb language plpgsql security definer set search_path = public as $$
declare r record; v_count integer:=0; v_fingerprints text[];
begin
  if current_setting('transaction_isolation')<>'read committed' then
    raise exception 'global history import requires READ COMMITTED' using errcode='25006';
  end if;
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
    public.visual_group_reconciliation, public.tenant_alias,
    public.visual_global_object_attestation,
    public.visual_global_scene_object_member, public.visual_global_object_lineage,
    public.visual_global_usage, public.visual_global_usage_member
    in share row exclusive mode nowait;
  if exists(select 1 from public.visual_group_reconciliation e
      where e.outcome='confirmed_published'
        and not exists(select 1 from public.visual_group_usage_ledger l
          where l.gym_id=e.gym_id and l.group_key=e.delivered_group_key
            and l.state='published')) then
    raise exception 'global history import refused: published event lacks ledger owner'
      using errcode='23514';
  end if;
  if exists(select 1 from public.visual_group_usage_ledger l
      where l.reserved_date is null and l.state<>'published') then
    raise exception 'global history import refused: staged history has no original date'
      using errcode='23514';
  end if;
  if exists(select 1 from public.visual_group_usage_ledger l
      where not public.visual_global_scene_complete(l.gym_id,l.group_key)) then
    raise exception 'global history import refused: occupied scene has incomplete byte evidence'
      using errcode='23514';
  end if;
  -- Validate every live calendar selection before making global writes. Rows
  -- whose complete fingerprint set is not yet imported report not_imported.
  if exists(select 1 from public.visual_global_coverage() c
      where c.issue not in ('ready','not_imported')) then
    raise exception 'global history import refused: calendar coverage incomplete'
      using errcode='23514';
  end if;
  for r in select l.* from public.visual_group_usage_ledger l
      order by (l.state='published') desc,l.gym_id,l.group_key loop
    select array_agg(fingerprint order by fingerprint) into v_fingerprints
      from public.visual_global_scene_fingerprints(r.gym_id,r.group_key);
    perform public.visual_global_claim_fingerprint_set(r.gym_id,r.group_key,
      r.reserved_date,r.calendar_row_id,r.channel,r.state='published',
      r.ambiguous,v_fingerprints);
    v_count:=v_count+1;
  end loop;
  if exists(select 1 from public.visual_global_coverage() where issue<>'ready')
      or exists(select 1 from public.visual_global_history_coverage() where issue<>'ready') then
    raise exception 'global history import refused: post-import coverage incomplete'
      using errcode='23514';
  end if;
  return jsonb_build_object('imported_local_groups',v_count,
    'global_fingerprints',(select count(*) from public.visual_global_usage));
end;
$$;

create or replace function public.visual_global_coverage()
returns table(calendar_row_id uuid,raw_gym_key text,tenant_id uuid,
  group_key text,fingerprint text,status text,post_date date,issue text)
language sql stable security definer set search_path = public as $$
  select c.id,c.gym_id,public.visual_group_tenant_id(c.gym_id),
    c.visual_group_key,fp.fingerprint,c.status,c.post_date,
    case
      when public.visual_group_tenant_id(c.gym_id) is null then 'unmapped_tenant'
      when c.visual_group_key is null then 'unresolved_group'
      when not public.visual_global_scene_complete(
          public.visual_group_tenant_id(c.gym_id)::text,c.visual_group_key)
        then 'incomplete_scene_byte_evidence'
      when fp.fingerprint is null then 'missing_verified_fingerprint'
      when not public.visual_global_row_bytes_verified(c)
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
      when g.fingerprint is null then 'not_imported'
      when g.tenant_id<>public.visual_group_tenant_id(c.gym_id)::text
          or g.used_date is distinct from c.post_date
        then 'global_owner_or_date_conflict'
      when m.fingerprint is null then 'missing_global_member'
      when m.used_date is distinct from c.post_date
          or ((c.status='published' or c.published_at is not null)
              and m.state<>'published')
          or (not (c.status='published' or c.published_at is not null)
              and m.state not in ('reserved','published'))
        then 'global_member_state_mismatch'
      else 'ready' end
  from public.content_calendar c
  left join lateral public.visual_global_scene_fingerprints(
    public.visual_group_tenant_id(c.gym_id)::text,c.visual_group_key) fp on true
  left join public.visual_global_usage g on g.fingerprint=fp.fingerprint
  left join public.visual_global_usage_member m
    on m.tenant_id=public.visual_group_tenant_id(c.gym_id)::text
      and m.group_key=c.visual_group_key and m.fingerprint=fp.fingerprint
  where c.status='published' or c.published_at is not null
    or public.visual_group_row_active(c) or public.visual_group_row_ambiguous(c);
$$;

-- Orphan ledger rows are covered independently of calendar rows. Every byte in
-- every linked component must retain an owner/member, including released rows.
create or replace function public.visual_global_history_coverage()
returns table(tenant_id text,group_key text,fingerprint text,
  used_date date,local_state text,issue text)
language sql stable security definer set search_path = public as $$
  select l.gym_id,l.group_key,fp.fingerprint,l.reserved_date,l.state,
    case
      when not public.visual_global_scene_complete(l.gym_id,l.group_key)
        then 'incomplete_scene_byte_evidence'
      when fp.fingerprint is null then 'missing_verified_fingerprint'
      when g.fingerprint is null then 'not_imported'
      when g.state='released' then 'legacy_released_global_usage'
      when g.tenant_id<>l.gym_id or g.used_date is distinct from l.reserved_date
        then 'global_owner_or_date_conflict'
      when m.fingerprint is null then 'missing_global_member'
      when m.state='released' then 'legacy_released_global_member'
      when m.used_date is distinct from l.reserved_date
          or m.state is distinct from case
            when l.state='published' then 'published' else 'reserved' end
        then 'global_member_state_mismatch'
      else 'ready' end
  from public.visual_group_usage_ledger l
  left join lateral public.visual_global_scene_fingerprints(
    l.gym_id,l.group_key) fp on true
  left join public.visual_global_usage g on g.fingerprint=fp.fingerprint
  left join public.visual_global_usage_member m
    on m.tenant_id=l.gym_id and m.group_key=l.group_key
      and m.fingerprint=fp.fingerprint
  union all
  -- A member left by an older one-fingerprint authority must correspond to a
  -- retained local ledger row and remain in that scene's complete byte set.
  select m.tenant_id,m.group_key,m.fingerprint,m.used_date,
    coalesce(l.state,'missing'),
    case when l.group_key is null then 'global_member_without_local_ledger'
      else 'global_member_not_in_current_scene' end
  from public.visual_global_usage_member m
  left join public.visual_group_usage_ledger l
    on l.gym_id=m.tenant_id and l.group_key=m.group_key
  where l.group_key is null or not exists(
    select 1 from public.visual_global_scene_fingerprints(
      m.tenant_id,m.group_key) fp where fp.fingerprint=m.fingerprint)
  union all
  -- Every permanent global owner must remain attributable to at least one
  -- immutable group member. Unknown orphan owners block activation; silently
  -- retaining them would hide incomplete legacy history from coverage.
  select g.tenant_id,null,g.fingerprint,g.used_date,g.state,
    'global_usage_without_member'
  from public.visual_global_usage g
  where not exists(select 1 from public.visual_global_usage_member m
    where m.fingerprint=g.fingerprint);
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
revoke all on function public.visual_global_prepare_bundle(text,jsonb,text,jsonb,text,text)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_prepare_source_rendition(text,text,uuid,uuid,uuid,text)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_immutable()
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_member_guard()
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_usage_guard()
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_exact_url_alias_guard()
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_row_fingerprint(public.content_calendar)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_group_bytes_verified(text,text,text)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_scene_fingerprints(text,text)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_scene_complete(text,text)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_row_bytes_verified(public.content_calendar)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_lineage_verified(text,text,text,text,text,text)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_claim_fingerprint_set(text,text,date,uuid,text,boolean,boolean,text[])
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_refresh_scene_history(text,text)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_scene_link_claim_guard()
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_claim_scene(public.content_calendar,boolean,boolean)
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
revoke all on function public.visual_global_block_local_activation()
  from public,anon,authenticated,service_role;
grant execute on function public.visual_global_register_identity(text,text,text,jsonb,text,text)
  to service_role;
grant execute on function public.visual_global_prepare_bundle(text,jsonb,text,jsonb,text,text)
  to service_role;
grant execute on function public.visual_global_prepare_source_rendition(text,text,uuid,uuid,uuid,text)
  to service_role;
grant execute on function public.visual_global_coverage() to service_role;
grant execute on function public.visual_global_history_coverage() to service_role;
commit;
