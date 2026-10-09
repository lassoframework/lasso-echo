-- Scoped source-brand schema. Release requires the normal migration gate.
-- No historical backfill, default brand, coach approval, or publication action.
create extension if not exists pgcrypto;

-- A trusted server capture adapter writes these; the browser cannot create captures.
-- raw_bytes are the exact fetched response bytes, not an excerpt or reconstructed text.
create table if not exists public.echo_source_captures (
  id uuid primary key default gen_random_uuid(),
  gym_id uuid not null references public.gyms(id),
  echo_account_key text not null check (length(echo_account_key) > 0),
  source_kind text not null check (source_kind in ('website','website_asset','social')),
  source_url text not null check (source_url ~ '^https://[^[:space:]]+$'),
  provider_account_id text,
  source_locator text,
  capture_provider text not null default 'direct' check (capture_provider in ('direct','apify')),
  provider_response_id text,
  source_revision text not null check (length(source_revision) > 0),
  mapping_revision text not null check (length(mapping_revision) > 0),
  mapping_evidence jsonb not null check (jsonb_typeof(mapping_evidence) = 'object' and mapping_evidence <> '{}'::jsonb),
  fetched_at timestamptz not null,
  raw_bytes bytea not null check (octet_length(raw_bytes) between 1 and 2000000),
  bytes_sha256 text generated always as (encode(digest(raw_bytes,'sha256'),'hex')) stored,
  captured_at timestamptz not null default clock_timestamp(),
  check (source_kind <> 'social' or (provider_account_id is not null and length(provider_account_id) > 0 and source_locator is not null and source_locator ~ '^https://[^[:space:]]+$')),
  check (capture_provider <> 'apify' or (source_kind='social' and source_url ~ '^https://api[.]apify[.]com/' and provider_response_id is not null and length(provider_response_id)>0)),
  check (fetched_at <= captured_at)
);
-- Server collector attestations of an authenticated COMPLETE provider account
-- listing. The display connection cache is never a negative source of truth.
create table if not exists public.echo_source_brand_provider_status (
  id bigint generated always as identity primary key,
  gym_id uuid not null references public.gyms(id),
  echo_account_key text not null check(length(echo_account_key)>0),
  attestation jsonb not null,
  request_id uuid not null unique,
  created_at timestamptz not null default clock_timestamp()
);
create table if not exists public.echo_source_brand_capability_events (
  id bigint generated always as identity primary key,
  gym_id uuid not null references public.gyms(id),
  clerk_user_id text not null,
  capability text not null default 'source_brand_approver' check (capability = 'source_brand_approver'),
  action text not null check (action in ('grant','revoke')),
  granted_by text not null,
  request_id uuid not null unique,
  created_at timestamptz not null default clock_timestamp()
);
create table if not exists public.echo_source_brand_bundles (
  id uuid primary key default gen_random_uuid(),
  gym_id uuid not null references public.gyms(id),
  echo_account_key text not null,
  schema_version integer not null default 1 check (schema_version in (1,2)),
  version integer not null check (version > 0),
  predecessor_id uuid references public.echo_source_brand_bundles(id),
  capture_ids uuid[] not null,
  snapshot_bytes text not null,
  content_sha256 text generated always as (encode(digest(snapshot_bytes,'sha256'),'hex')) stored,
  source_revision text not null,
  palette_revision text not null,
  created_at timestamptz not null default clock_timestamp(),
  unique(gym_id, version), unique(id, gym_id)
);
-- Additive draft upgrade also supports fixtures installed from the v1 draft.
alter table public.echo_source_brand_bundles drop constraint if exists echo_source_brand_bundles_schema_version_check;
alter table public.echo_source_brand_bundles add constraint echo_source_brand_bundles_schema_version_check check(schema_version in (1,2));
create table if not exists public.echo_source_brand_observations (
  id bigint generated always as identity primary key,
  gym_id uuid not null,
  bundle_id uuid not null,
  configuration_sha256 text not null,
  snapshot_bytes text not null,
  content_sha256 text generated always as (encode(digest(snapshot_bytes,'sha256'),'hex')) stored,
  validator_revision text not null check(length(validator_revision)>0),
  validation_report jsonb not null,
  created_at timestamptz not null default clock_timestamp(),
  foreign key(bundle_id,gym_id) references public.echo_source_brand_bundles(id,gym_id)
);
create table if not exists public.echo_source_brand_receipts (
  id bigint generated always as identity primary key,
  gym_id uuid not null,
  bundle_id uuid not null,
  bundle_version integer not null,
  content_sha256 text not null,
  actor_clerk_user_id text not null,
  actor_authority text not null check (actor_authority in ('blake','source_brand_approver')),
  purpose text not null default 'echo_source_brand_configuration' check (purpose = 'echo_source_brand_configuration'),
  action text not null check (action in ('approve','revoke','supersede')),
  request_id uuid not null unique,
  created_at timestamptz not null default clock_timestamp(),
  foreign key (bundle_id,gym_id) references public.echo_source_brand_bundles(id,gym_id)
);

create or replace function public.echo_source_brand_immutable() returns trigger language plpgsql as $$
begin raise exception 'append_only_evidence'; end $$;
do $$ declare t text; begin
  foreach t in array array['echo_source_captures','echo_source_brand_provider_status','echo_source_brand_capability_events','echo_source_brand_bundles','echo_source_brand_receipts','echo_source_brand_observations'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on public.%I from anon, authenticated', t);
    execute format('drop trigger if exists %I on public.%I', t || '_immutable', t);
    execute format('create trigger %I before update or delete on public.%I for each row execute function public.echo_source_brand_immutable()', t || '_immutable', t);
  end loop;
end $$;

-- Direct trusted capture inserts participate in approval/revalidation ordering.
-- Invoker privileges suffice; this does not grant access or bypass tenant RLS.
create or replace function public.echo_source_capture_insert_lock() returns trigger
language plpgsql set search_path = public,pg_temp as $$
begin
  if new.captured_at>clock_timestamp() or new.fetched_at>new.captured_at then
    raise exception 'invalid_capture_timestamp';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(new.gym_id::text,0));
  -- The default can be evaluated before a lock wait. Stamp database arrival
  -- after acquiring the lock so latest-capture order follows serialization.
  new.captured_at := clock_timestamp();
  return new;
end $$;
revoke all on function public.echo_source_capture_insert_lock() from public,anon,authenticated;
drop trigger if exists echo_source_capture_insert_lock on public.echo_source_captures;
create trigger echo_source_capture_insert_lock before insert on public.echo_source_captures
for each row execute function public.echo_source_capture_insert_lock();

-- Deliberately fails closed when Clerk is absent. Only Blake or a gym-scoped grant.
create or replace function public.echo_source_brand_authority(p_actor text,p_gym uuid)
returns text language plpgsql security definer set search_path = public,pg_temp as $$
declare u public.app_users; a text;
begin
  select * into u from public.app_users where clerk_user_id = p_actor;
  if u.id is null then raise exception 'authentication_required'; end if;
  if u.role = 'owner' and lower(u.email) = 'blake@lassoframework.com' then return 'blake'; end if;
  if u.role not in ('owner','executive','admin_no_financials') then raise exception 'approver_required'; end if;
  select action into a from public.echo_source_brand_capability_events where gym_id=p_gym and clerk_user_id=p_actor order by id desc limit 1;
  if a = 'grant' then return 'source_brand_approver'; end if;
  raise exception 'approver_required';
end $$;

-- Exact selected facts; callers supply offsets, never unsupported fact text.
create or replace function public.echo_source_brand_fact_spans(p_gym uuid,p_key text,p_ids uuid[],p_spans jsonb)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare span jsonb; c public.echo_source_captures; result jsonb:='[]'::jsonb; text_value text;
begin
  if p_spans is null or jsonb_typeof(p_spans)<>'array' or jsonb_array_length(p_spans)>30 then raise exception 'fact_evidence_invalid'; end if;
  for span in select value from jsonb_array_elements(p_spans) loop
    if span->>'key' is null or length(span->>'key') not between 1 and 100 or (span->>'byte_offset')::integer<0 or (span->>'byte_length')::integer not between 1 and 2000 or span->>'byte_offset' is null or span->>'byte_length' is null or exists(select 1 from jsonb_array_elements(result) x where x->>'key'=span->>'key') then raise exception 'fact_evidence_invalid'; end if;
    select * into c from echo_source_captures where id=(span->>'capture_id')::uuid and id=any(p_ids) and gym_id=p_gym and echo_account_key=p_key;
    if c.id is null or (span->>'byte_offset')::bigint+(span->>'byte_length')::integer>octet_length(c.raw_bytes) then raise exception 'fact_evidence_invalid'; end if;
    begin
      text_value:=convert_from(substring(c.raw_bytes from (span->>'byte_offset')::integer+1 for (span->>'byte_length')::integer),'UTF8');
    exception when character_not_in_repertoire then
      raise exception 'fact_evidence_invalid';
    end;
    result:=result||jsonb_build_array(jsonb_build_object('key',span->>'key','capture_id',c.id,'bytes_sha256',c.bytes_sha256,'source_locator',coalesce(c.source_locator,c.source_url),'byte_offset',(span->>'byte_offset')::integer,'byte_length',(span->>'byte_length')::integer,'text',text_value));
  end loop;
  return result;
end $$;
revoke all on function public.echo_source_brand_fact_spans(uuid,text,uuid[],jsonb) from public,anon,authenticated,service_role;

-- Only the reviewed server collector can attest provider evidence. A failed or
-- partial lookup supersedes older success with a hold; it cannot invent absence.
create or replace function public.echo_source_brand_attest_provider(p_gym uuid,p_key text,p_status jsonb,p_request uuid)
returns public.echo_source_brand_provider_status language plpgsql security definer set search_path=public,pg_temp as $$
declare r public.echo_source_brand_provider_status; ig jsonb;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_gym::text,0));
  -- Exact retries return their immutable receipt and never append a new current row.
  select * into r from echo_source_brand_provider_status where request_id=p_request;
  if r.id is not null then
    if r.gym_id=p_gym and r.echo_account_key=p_key and r.attestation=p_status then return r; end if;
    raise exception 'request_id_conflict';
  end if;
  if p_key is null or p_key is distinct from (select echo_account_key from echo_intake_tokens where gym_id=p_gym) then raise exception 'tenant_mapping_missing'; end if;
  if p_request is null or p_status is null or jsonb_typeof(p_status)<>'object' or p_status->>'gym_id' is distinct from p_gym::text or p_status->>'echo_account_key' is distinct from p_key
    or p_status->>'provider' is distinct from 'zernio' or p_status->>'source' is distinct from 'zernio_authenticated_accounts'
    or coalesce(length(p_status->>'mapping_revision'),0)=0
    or coalesce(p_status->>'lookup_status','') not in ('complete','partial','unavailable') or jsonb_typeof(p_status->'authenticated') is distinct from 'boolean'
    or p_status->>'observed_at' is null or (p_status->>'observed_at')::timestamptz>clock_timestamp() then raise exception 'provider_attestation_invalid'; end if;
  ig:=p_status->'instagram';
  if p_status->>'lookup_status'='complete' then
    if coalesce(length(p_status->>'profile_id'),0)=0 or p_status->>'authenticated' is distinct from 'true' or coalesce(p_status->>'response_sha256','') !~ '^[a-f0-9]{64}$' or jsonb_typeof(ig) is distinct from 'object'
      or jsonb_typeof(ig->'connected') is distinct from 'boolean' then raise exception 'provider_attestation_invalid'; end if;
    if ig->>'connected'='true' then
      if coalesce(length(ig->>'account_id'),0)=0 or coalesce(ig->>'platform_user_id','') !~ '^[0-9]+$' or coalesce(length(ig->>'handle'),0)=0 then raise exception 'provider_attestation_invalid'; end if;
    elsif ig->'account_id' is distinct from 'null'::jsonb or ig->'platform_user_id' is distinct from 'null'::jsonb or ig->'handle' is distinct from 'null'::jsonb then raise exception 'provider_attestation_invalid'; end if;
  end if;
  -- observed_at is the provider lookup START time. A delayed old response must
  -- never supersede a newer connected/failed/partial lookup, including equal time.
  if exists(select 1 from echo_source_brand_provider_status latest where latest.gym_id=p_gym
    and (latest.attestation->>'observed_at')::timestamptz >= (p_status->>'observed_at')::timestamptz) then raise exception 'provider_attestation_out_of_order'; end if;
  insert into echo_source_brand_provider_status(gym_id,echo_account_key,attestation,request_id) values(p_gym,p_key,p_status,p_request) returning * into r;
  return r;
end $$;
revoke all on function public.echo_source_brand_attest_provider(uuid,text,jsonb,uuid) from public,anon,authenticated;
grant execute on function public.echo_source_brand_attest_provider(uuid,text,jsonb,uuid) to service_role;

-- Latest attempted lookup is authoritative; a failure does not reuse an older
-- negative. Identity excludes heartbeat timestamps, but availability expires.
create or replace function public.echo_source_brand_provider_current(p_gym uuid,p_key text)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare r public.echo_source_brand_provider_status;
begin
  select * into r from echo_source_brand_provider_status where gym_id=p_gym order by id desc limit 1;
  if r.id is null or r.echo_account_key is distinct from p_key or r.attestation->>'lookup_status' is distinct from 'complete' or r.attestation->>'authenticated' is distinct from 'true'
    or (r.attestation->>'observed_at')::timestamptz not between clock_timestamp()-interval '15 minutes' and clock_timestamp() then return null; end if;
  return to_jsonb(r);
end $$;
revoke all on function public.echo_source_brand_provider_current(uuid,text) from public,anon,authenticated,service_role;

-- Derive identity from one already scoped provider read. This helper never reads
-- provider state again; active readback validates and returns the same attestation.
create or replace function public.echo_source_brand_policy_from_report(p_gym uuid,p_key text,p_mode text,report jsonb)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare connection jsonb;
begin
  if p_mode is null or p_mode not in ('website_and_social_v2','website_only_no_connected_instagram_v2') then raise exception 'source_policy_invalid'; end if;
  if report is null or report->>'gym_id' is distinct from p_gym::text or report->>'echo_account_key' is distinct from p_key
    or report->>'lookup_status' is distinct from 'complete' or report->>'authenticated' is distinct from 'true'
    or report->>'observed_at' is null or (report->>'observed_at')::timestamptz not between clock_timestamp()-interval '15 minutes' and clock_timestamp() then raise exception 'provider_status_unavailable'; end if;
  if report->'instagram'->'connected' is distinct from 'true'::jsonb
    and report->'instagram'->'connected' is distinct from 'false'::jsonb then raise exception 'provider_status_unavailable'; end if;
  if report->'instagram'->'connected'='true'::jsonb then
    connection:=jsonb_build_object('id',report->'instagram'->>'account_id','platform_user_id',report->'instagram'->>'platform_user_id','handle',report->'instagram'->>'handle');
    if p_mode='website_only_no_connected_instagram_v2' then raise exception 'connected_instagram_requires_social'; end if;
  elsif p_mode='website_and_social_v2' then
    raise exception 'source_policy_invalid';
  end if;
  return jsonb_build_object('version',2,'mode',p_mode,'gym_id',p_gym,'echo_account_key',p_key,
    'provider_identity',jsonb_build_object('provider',report->>'provider','source',report->>'source','profile_id',report->>'profile_id','mapping_revision',report->>'mapping_revision'),'instagram_connection',connection);
end $$;
revoke all on function public.echo_source_brand_policy_from_report(uuid,text,text,jsonb) from public,anon,authenticated,service_role;
create or replace function public.echo_source_brand_policy(p_gym uuid,p_key text,p_mode text)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
begin
  if p_key is null or p_key is distinct from (select echo_account_key from echo_intake_tokens where gym_id=p_gym) then raise exception 'tenant_mapping_missing'; end if;
  return echo_source_brand_policy_from_report(p_gym,p_key,p_mode,echo_source_brand_provider_current(p_gym,p_key)->'attestation');
end $$;
revoke all on function public.echo_source_brand_policy(uuid,text,text) from public,anon,authenticated,service_role;

-- Bind the immutable provider response used for each snapshot independently of
-- stable identity, so routine fresh lookups do not change approved policy.
create or replace function public.echo_source_brand_provider_receipt(p_gym uuid,p_key text)
returns jsonb language sql security definer set search_path=public,pg_temp as $$
  select jsonb_build_object('id',r->'id','observed_at',r->'attestation'->>'observed_at','response_sha256',r->'attestation'->>'response_sha256') from (select echo_source_brand_provider_current(p_gym,p_key) r) x where r is not null;
$$;
revoke all on function public.echo_source_brand_provider_receipt(uuid,text) from public,anon,authenticated,service_role;
create or replace function public.echo_source_brand_provider_receipt_valid(p_gym uuid,p_key text,p_receipt jsonb,p_policy jsonb)
returns boolean language sql security definer set search_path=public,pg_temp as $$
  select exists(select 1 from echo_source_brand_provider_status r where r.id::text=p_receipt->>'id' and r.gym_id=p_gym and r.echo_account_key=p_key
    and r.attestation->>'observed_at'=p_receipt->>'observed_at' and r.attestation->>'response_sha256'=p_receipt->>'response_sha256'
    and r.attestation->>'lookup_status'='complete' and r.attestation->>'authenticated'='true'
    and jsonb_build_object('provider',r.attestation->>'provider','source',r.attestation->>'source','profile_id',r.attestation->>'profile_id','mapping_revision',r.attestation->>'mapping_revision')=p_policy->'provider_identity'
    and case when r.attestation->'instagram'->>'connected'='true' then jsonb_build_object('id',r.attestation->'instagram'->>'account_id','platform_user_id',r.attestation->'instagram'->>'platform_user_id','handle',r.attestation->'instagram'->>'handle') else 'null'::jsonb end=p_policy->'instagram_connection');
$$;
revoke all on function public.echo_source_brand_provider_receipt_valid(uuid,text,jsonb,jsonb) from public,anon,authenticated,service_role;

-- Missing mapping or provider evidence holds preparation, never management revoke.
create or replace function public.echo_source_brand_current_policy(p_gym uuid)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare k text; report jsonb;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_gym::text,0));
  select echo_account_key into k from echo_intake_tokens where gym_id=p_gym;
  if k is null or length(k)=0 then return null; end if;
  report:=echo_source_brand_provider_current(p_gym,k)->'attestation';
  if report is null then return null; end if;
  return echo_source_brand_policy_from_report(p_gym,k,case when report->'instagram'->>'connected'='true' then 'website_and_social_v2' else 'website_only_no_connected_instagram_v2' end,report);
end $$;
revoke all on function public.echo_source_brand_current_policy(uuid) from public,anon,authenticated;
grant execute on function public.echo_source_brand_current_policy(uuid) to service_role;

create or replace function public.echo_source_brand_prepare(p_gym uuid,p_actor text,p_capture_ids uuid[],p_palette_capture uuid,p_primary_offset integer,p_secondary_offset integer,p_predecessor uuid,p_fact_spans jsonb,p_source_policy text)
returns public.echo_source_brand_bundles language plpgsql security definer set search_path = public,pg_temp as $$
declare k text; policy jsonb; frozen jsonb; facts jsonb; snapshots jsonb; pal public.echo_source_captures; primary_color text; secondary_color text; previous public.echo_source_brand_bundles; result public.echo_source_brand_bundles;
begin
  if current_setting('server_encoding') <> 'UTF8' then raise exception 'utf8_database_required'; end if;
  perform public.echo_source_brand_authority(p_actor,p_gym);
  perform pg_advisory_xact_lock(hashtextextended(p_gym::text,0));
  select echo_account_key into k from public.echo_intake_tokens where gym_id=p_gym;
  if k is null or length(k)=0 then raise exception 'tenant_mapping_missing'; end if;
  if p_source_policy is not null then policy:=echo_source_brand_policy(p_gym,k,p_source_policy); end if;
  if p_capture_ids is null or array_position(p_capture_ids,null) is not null or cardinality(p_capture_ids) not between (case when policy is null or p_source_policy='website_and_social_v2' then 2 else 1 end) and 12 or cardinality(p_capture_ids) <> (select count(distinct x) from unnest(p_capture_ids) x) then raise exception 'capture_set_invalid'; end if;
  if (select count(*) from public.echo_source_captures where id=any(p_capture_ids) and gym_id=p_gym and echo_account_key=k and fetched_at between clock_timestamp()-interval '7 days' and clock_timestamp()) <> cardinality(p_capture_ids) then raise exception 'missing_stale_or_cross_tenant_capture'; end if;
  if (select count(distinct (source_kind,coalesce(source_locator,source_url))) from echo_source_captures where id=any(p_capture_ids) and gym_id=p_gym and echo_account_key=k)<>cardinality(p_capture_ids) then raise exception 'duplicate_source_identity'; end if;
  if policy is null or p_source_policy='website_and_social_v2' then
    if not exists(select 1 from public.echo_source_captures where id=any(p_capture_ids) and source_kind='website') or not exists(select 1 from public.echo_source_captures where id=any(p_capture_ids) and source_kind='social') then raise exception 'website_and_social_required'; end if;
  else
    if not exists(select 1 from echo_source_captures where id=any(p_capture_ids) and source_kind='website') or exists(select 1 from echo_source_captures where id=any(p_capture_ids) and source_kind='social') then raise exception 'website_only_capture_set_required'; end if;
  end if;
  if policy->'instagram_connection' is distinct from 'null'::jsonb and policy is not null and exists(select 1 from echo_source_captures where id=any(p_capture_ids) and source_kind='social' and provider_account_id is distinct from policy->'instagram_connection'->>'platform_user_id') then raise exception 'provider_instagram_identity_mismatch'; end if;
  select * into pal from public.echo_source_captures where id=p_palette_capture and id=any(p_capture_ids) and gym_id=p_gym and echo_account_key=k and source_kind in ('website','website_asset');
  if pal.id is null or p_primary_offset is null or p_secondary_offset is null or p_primary_offset < 0 or p_secondary_offset < 0 then raise exception 'palette_evidence_missing'; end if;
  -- Check the entire byte window with bigint before the int4 substring start addition.
  if p_primary_offset::bigint+7>octet_length(pal.raw_bytes) or p_secondary_offset::bigint+7>octet_length(pal.raw_bytes) then raise exception 'palette_evidence_invalid'; end if;
  -- Zero-based BYTE offsets into immutable raw response bytes. No brand defaults.
  begin
    primary_color := convert_from(substring(pal.raw_bytes from p_primary_offset+1 for 7),'UTF8');
    secondary_color := convert_from(substring(pal.raw_bytes from p_secondary_offset+1 for 7),'UTF8');
  exception when character_not_in_repertoire then
    raise exception 'palette_evidence_invalid';
  end;
  if primary_color is null or secondary_color is null or primary_color !~ '^#[0-9A-Fa-f]{6}$' or secondary_color !~ '^#[0-9A-Fa-f]{6}$' then raise exception 'palette_evidence_invalid'; end if;
  facts := public.echo_source_brand_fact_spans(p_gym,k,p_capture_ids,p_fact_spans);
  select * into previous from public.echo_source_brand_bundles where gym_id=p_gym order by version desc limit 1;
  if previous.id is distinct from p_predecessor then raise exception 'stale_predecessor'; end if;
  select jsonb_agg(jsonb_build_object('id',id,'gym_id',gym_id,'echo_account_key',echo_account_key,'source_kind',source_kind,'source_url',source_url,'source_locator',source_locator,'capture_provider',capture_provider,'provider_response_id',provider_response_id,'provider_account_id',provider_account_id,'source_revision',source_revision,'mapping_revision',mapping_revision,'mapping_evidence',mapping_evidence,'fetched_at',fetched_at,'bytes_sha256',bytes_sha256,'bytes_base64',replace(encode(raw_bytes,'base64'),chr(10),'')) order by id) into snapshots from public.echo_source_captures where id=any(p_capture_ids) and gym_id=p_gym and echo_account_key=k;
  frozen:=jsonb_build_object('schema_version',1,'gym_id',p_gym,'echo_account_key',k,'captures',snapshots,'fact_policy','delegated_supported_facts','selected_facts',facts,'palette',jsonb_build_object('capture_id',pal.id,'bytes_sha256',pal.bytes_sha256,'primary',primary_color,'secondary',secondary_color,'primary_byte_offset',p_primary_offset,'secondary_byte_offset',p_secondary_offset));
  if policy is not null then frozen:=frozen||jsonb_build_object('schema_version',2,'source_policy',policy,'provider_status_receipt',echo_source_brand_provider_receipt(p_gym,k)); end if;
  insert into public.echo_source_brand_bundles(gym_id,echo_account_key,schema_version,version,predecessor_id,capture_ids,snapshot_bytes,source_revision,palette_revision)
  values(p_gym,k,(frozen->>'schema_version')::integer,coalesce(previous.version,0)+1,previous.id,p_capture_ids,frozen::text,
    encode(digest(convert_to(snapshots::text,'UTF8'),'sha256'),'hex'),pal.bytes_sha256 || ':' || p_primary_offset || ':' || p_secondary_offset)
  returning * into result;
  return result;
end $$;

-- Preserve the v1 RPC signature, default fact spans and exact snapshot shape.
create or replace function public.echo_source_brand_prepare(p_gym uuid,p_actor text,p_capture_ids uuid[],p_palette_capture uuid,p_primary_offset integer,p_secondary_offset integer,p_predecessor uuid,p_fact_spans jsonb default '[]'::jsonb)
returns public.echo_source_brand_bundles language sql security definer set search_path=public,pg_temp as $$
  select echo_source_brand_prepare(p_gym,p_actor,p_capture_ids,p_palette_capture,p_primary_offset,p_secondary_offset,p_predecessor,p_fact_spans,null);
$$;
revoke all on function public.echo_source_brand_prepare(uuid,text,uuid[],uuid,integer,integer,uuid,jsonb,text) from public,anon,authenticated;
grant execute on function public.echo_source_brand_prepare(uuid,text,uuid[],uuid,integer,integer,uuid,jsonb,text) to service_role;

create or replace function public.echo_source_brand_decide(p_gym uuid,p_actor text,p_bundle uuid,p_hash text,p_version integer,p_request uuid,p_action text)
returns public.echo_source_brand_receipts language plpgsql security definer set search_path = public,pg_temp as $$
declare authority text; b public.echo_source_brand_bundles; r public.echo_source_brand_receipts; latest_id uuid; k text;
begin
  authority := public.echo_source_brand_authority(p_actor,p_gym);
  if p_action not in ('approve','revoke') then raise exception 'invalid_action'; end if;
  perform pg_advisory_xact_lock(hashtextextended(p_gym::text,0));
  select * into r from public.echo_source_brand_receipts where request_id=p_request;
  if r.id is not null then
    if r.gym_id=p_gym and r.actor_clerk_user_id=p_actor and r.bundle_id=p_bundle and r.content_sha256=p_hash and r.bundle_version=p_version and r.action=p_action then return r; end if;
    raise exception 'request_id_conflict';
  end if;
  select * into b from public.echo_source_brand_bundles where id=p_bundle and gym_id=p_gym;
  if b.id is null or p_hash is null or p_version is null or b.content_sha256<>p_hash or b.version<>p_version then raise exception 'stale_displayed_snapshot'; end if;
  if p_action='approve' then
    select id into latest_id from public.echo_source_brand_bundles where gym_id=p_gym order by version desc limit 1;
    select echo_account_key into k from public.echo_intake_tokens where gym_id=p_gym;
    if latest_id<>b.id or k is distinct from b.echo_account_key then raise exception 'stale_displayed_snapshot'; end if;
    if b.schema_version=2 and b.snapshot_bytes::jsonb->'source_policy' is distinct from echo_source_brand_policy(p_gym,k,b.snapshot_bytes::jsonb->'source_policy'->>'mode') then raise exception 'stale_source_policy'; end if;
    if b.schema_version=2 and not echo_source_brand_provider_receipt_valid(p_gym,k,b.snapshot_bytes::jsonb->'provider_status_receipt',b.snapshot_bytes::jsonb->'source_policy') then raise exception 'provider_attestation_invalid'; end if;
    if exists(select 1 from public.echo_source_captures c where c.id=any(b.capture_ids) and (c.fetched_at<clock_timestamp()-interval '7 days' or c.fetched_at>clock_timestamp() or exists(select 1 from public.echo_source_captures newer where newer.gym_id=p_gym and (newer.source_kind=c.source_kind and coalesce(newer.source_locator,newer.source_url)=coalesce(c.source_locator,c.source_url) or (c.source_kind='social' and newer.provider_account_id=c.provider_account_id)) and newer.captured_at>c.captured_at))) then raise exception 'stale_source_capture'; end if;
    if b.predecessor_id is not null then
      insert into public.echo_source_brand_receipts(gym_id,bundle_id,bundle_version,content_sha256,actor_clerk_user_id,actor_authority,action,request_id)
      select prior.gym_id,prior.id,prior.version,prior.content_sha256,p_actor,authority,'supersede',gen_random_uuid() from public.echo_source_brand_bundles prior where prior.gym_id=p_gym and prior.version<b.version and exists(select 1 from echo_source_brand_receipts where bundle_id=prior.id and action='approve') and not exists(select 1 from echo_source_brand_receipts where bundle_id=prior.id and action='supersede');
    end if;
  end if;
  insert into public.echo_source_brand_receipts(gym_id,bundle_id,bundle_version,content_sha256,actor_clerk_user_id,actor_authority,action,request_id)
  values(p_gym,b.id,b.version,b.content_sha256,p_actor,authority,p_action,p_request) returning * into r;
  return r;
end $$;

-- Only the authenticated portal server can invoke these; actor comes from Clerk.
revoke all on function public.echo_source_brand_authority(text,uuid) from public,anon,authenticated;
revoke all on function public.echo_source_brand_prepare(uuid,text,uuid[],uuid,integer,integer,uuid,jsonb) from public,anon,authenticated;
revoke all on function public.echo_source_brand_decide(uuid,text,uuid,text,integer,uuid,text) from public,anon,authenticated;
grant execute on function public.echo_source_brand_authority(text,uuid) to service_role;
grant execute on function public.echo_source_brand_prepare(uuid,text,uuid[],uuid,integer,integer,uuid,jsonb) to service_role;
grant execute on function public.echo_source_brand_decide(uuid,text,uuid,text,integer,uuid,text) to service_role;

-- Configuration readback remains available for revocation during observation holds.
create or replace function public.echo_source_brand_configuration(p_gym uuid)
returns jsonb language sql security definer set search_path=public,pg_temp as $$
  select jsonb_build_object('bundle',to_jsonb(b),'approval_receipt',to_jsonb(r))
  from echo_source_brand_bundles b
  join lateral (select * from echo_source_brand_receipts where bundle_id=b.id order by id desc limit 1) r on r.action='approve'
  where b.gym_id=p_gym order by b.version desc limit 1;
$$;
revoke all on function public.echo_source_brand_configuration(uuid) from public,anon,authenticated;
grant execute on function public.echo_source_brand_configuration(uuid) to service_role;

-- Durable configuration and immutable observations have independent lifetimes.
-- A draft successor alone cannot revoke approved configuration.
create or replace function public.echo_source_brand_active(p_gym uuid)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare b public.echo_source_brand_bundles; r public.echo_source_brand_receipts; k text; provider_status jsonb; observation jsonb; o public.echo_source_brand_observations; c jsonb; newest public.echo_source_captures;
begin
  -- The attestor and configuration decisions take this same transaction lock.
  -- Serialize the whole response so a newer lookup cannot replace checked state.
  perform pg_advisory_xact_lock(hashtextextended(p_gym::text,0));
  select x.* into b from public.echo_source_brand_bundles x
  join lateral (select action from public.echo_source_brand_receipts where bundle_id=x.id order by id desc limit 1) decision on decision.action='approve'
  where x.gym_id=p_gym order by x.version desc limit 1;
  if b.id is null then return null; end if;
  select * into r from public.echo_source_brand_receipts where bundle_id=b.id order by id desc limit 1;
  if r.content_sha256<>b.content_sha256 or r.bundle_version<>b.version then return null; end if;
  begin perform public.echo_source_brand_authority(r.actor_clerk_user_id,p_gym); exception when others then return null; end;
  select echo_account_key into k from public.echo_intake_tokens where gym_id=p_gym;
  if k is distinct from b.echo_account_key then return null; end if;
  if b.schema_version=2 then
    provider_status:=echo_source_brand_provider_current(p_gym,k)->'attestation';
    begin
      if b.snapshot_bytes::jsonb->'source_policy' is distinct from echo_source_brand_policy_from_report(p_gym,k,b.snapshot_bytes::jsonb->'source_policy'->>'mode',provider_status) then return null; end if;
    exception when others then return null; end;
  end if;
  select * into o from public.echo_source_brand_observations where bundle_id=b.id order by id desc limit 1;
  if o.id is not null and (o.configuration_sha256<>b.content_sha256 or o.validation_report->>'selected_facts_status' is distinct from 'supported_uncontradicted' or o.validation_report->>'identity_status' is distinct from 'verified') then return null; end if;
  observation := case when o.id is null then b.snapshot_bytes::jsonb else o.snapshot_bytes::jsonb end;
  if b.schema_version=2 then
    if not echo_source_brand_provider_receipt_valid(p_gym,k,observation->'provider_status_receipt',observation->'source_policy') then return null; end if;
    if observation->'source_policy'->'instagram_connection' is distinct from 'null'::jsonb and exists(select 1 from jsonb_array_elements(observation->'captures') x where x->>'source_kind'='social' and x->>'provider_account_id' is distinct from observation->'source_policy'->'instagram_connection'->>'platform_user_id') then return null; end if;
    if observation->>'schema_version' is distinct from '2' or observation->'source_policy' is distinct from b.snapshot_bytes::jsonb->'source_policy'
      or not exists(select 1 from jsonb_array_elements(observation->'captures') x where x->>'source_kind'='website') then return null; end if;
    if observation->'source_policy'->>'mode'='website_only_no_connected_instagram_v2' then
      if exists(select 1 from jsonb_array_elements(observation->'captures') x where x->>'source_kind'='social') then return null; end if;
    elsif not exists(select 1 from jsonb_array_elements(observation->'captures') x where x->>'source_kind'='social') then return null; end if;
  end if;
  if observation->>'gym_id' is distinct from p_gym::text or observation->>'echo_account_key' is distinct from k or (select count(distinct (x->>'source_kind',coalesce(x->>'source_locator',x->>'source_url'))) from jsonb_array_elements(observation->'captures') x)<>jsonb_array_length(observation->'captures') then return null; end if;
  for c in select value from jsonb_array_elements(observation->'captures') loop
    if c->>'gym_id' is distinct from p_gym::text or c->>'echo_account_key' is distinct from k or not exists(select 1 from echo_source_captures actual where actual.id::text=c->>'id' and actual.gym_id=p_gym and actual.echo_account_key=k and actual.bytes_sha256=c->>'bytes_sha256') then return null; end if;
    -- Latest locator observation is authoritative even if its tenant identity drifted.
    select * into newest from public.echo_source_captures where gym_id=p_gym and source_kind=c->>'source_kind'
      and coalesce(source_locator,source_url)=coalesce(c->>'source_locator',c->>'source_url') order by captured_at desc,id desc limit 1;
    if newest.id is null or newest.echo_account_key<>k or newest.provider_account_id is distinct from c->>'provider_account_id'
      or newest.capture_provider<>c->>'capture_provider' or newest.mapping_revision<>c->>'mapping_revision' or newest.mapping_evidence<>c->'mapping_evidence'
      or newest.bytes_sha256<>c->>'bytes_sha256' or newest.fetched_at<clock_timestamp()-interval '7 days' or newest.fetched_at>clock_timestamp() then return null; end if;
  end loop;
  -- Evidence can expire while other source checks run; never emit expired proof.
  if b.schema_version=2 and (provider_status is null or provider_status->>'lookup_status' is distinct from 'complete' or provider_status->>'authenticated' is distinct from 'true'
    or provider_status->>'observed_at' is null or (provider_status->>'observed_at')::timestamptz not between clock_timestamp()-interval '15 minutes' and clock_timestamp()) then return null; end if;
  return jsonb_build_object('bundle',to_jsonb(b),'approval_receipt',to_jsonb(r),
    'observation',case when o.id is null then null else to_jsonb(o) end,
    'fact_approval_mode','delegated_policy','fact_validation',case when o.id is null then 'pending_collector_validation' else 'supported_uncontradicted' end)
    || case when b.schema_version=2 then jsonb_build_object('provider_status',provider_status) else '{}'::jsonb end;
end $$;
revoke all on function public.echo_source_brand_active(uuid) from public,anon,authenticated;
grant execute on function public.echo_source_brand_active(uuid) to service_role;

-- Only a reviewed trusted producer may call this service-only adapter. This is a
-- validator attestation with exact byte witnesses, not database proof of semantics.
create or replace function public.echo_source_brand_revalidate(p_gym uuid,p_bundle uuid,p_hash text,p_capture_ids uuid[],p_palette_capture uuid,p_primary_offset integer,p_secondary_offset integer,p_fact_spans jsonb,p_validator_revision text,p_validation_report jsonb)
returns public.echo_source_brand_observations language plpgsql security definer set search_path=public,pg_temp as $$
declare b public.echo_source_brand_bundles; snapshots jsonb; facts jsonb; pal public.echo_source_captures; result public.echo_source_brand_observations; frozen jsonb; old jsonb; c jsonb; current_capture public.echo_source_captures; primary_color text; secondary_color text;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_gym::text,0));
  select * into b from public.echo_source_brand_bundles where id=p_bundle and gym_id=p_gym;
  if b.id is null or p_hash is distinct from b.content_sha256 or b.echo_account_key is distinct from (select echo_account_key from echo_intake_tokens where gym_id=p_gym) then raise exception 'stale_configuration'; end if;
  if (select action from echo_source_brand_receipts where bundle_id=b.id order by id desc limit 1) is distinct from 'approve' then raise exception 'configuration_not_approved'; end if;
  if b.schema_version=2 and b.snapshot_bytes::jsonb->'source_policy' is distinct from echo_source_brand_policy(p_gym,b.echo_account_key,b.snapshot_bytes::jsonb->'source_policy'->>'mode') then raise exception 'stale_source_policy'; end if;
  if b.schema_version=2 and not echo_source_brand_provider_receipt_valid(p_gym,b.echo_account_key,b.snapshot_bytes::jsonb->'provider_status_receipt',b.snapshot_bytes::jsonb->'source_policy') then raise exception 'provider_attestation_invalid'; end if;
  perform echo_source_brand_authority((select actor_clerk_user_id from echo_source_brand_receipts where bundle_id=b.id order by id desc limit 1),p_gym);
  if p_validator_revision is null or length(p_validator_revision)=0 or p_validation_report is null or coalesce(p_validation_report->>'selected_facts_status','') not in ('supported_uncontradicted','missing','contradicted') or coalesce(p_validation_report->>'identity_status','') not in ('verified','changed','missing') then raise exception 'validation_required'; end if;
  if p_validation_report->>'selected_facts_status'<>'supported_uncontradicted' or p_validation_report->>'identity_status'<>'verified' then
    insert into echo_source_brand_observations(gym_id,bundle_id,configuration_sha256,snapshot_bytes,validator_revision,validation_report) values(p_gym,b.id,b.content_sha256,b.snapshot_bytes,p_validator_revision,p_validation_report) returning * into result;
    return result;
  end if;
  if p_capture_ids is null or array_position(p_capture_ids,null) is not null or cardinality(p_capture_ids)<>jsonb_array_length(b.snapshot_bytes::jsonb->'captures') or cardinality(p_capture_ids)<>(select count(distinct x) from unnest(p_capture_ids) x) then raise exception 'capture_set_invalid'; end if;
  if (select count(*) from echo_source_captures where id=any(p_capture_ids) and gym_id=p_gym and echo_account_key=b.echo_account_key and fetched_at between clock_timestamp()-interval '7 days' and clock_timestamp())<>cardinality(p_capture_ids) then raise exception 'missing_stale_or_cross_tenant_capture'; end if;
  if (select count(distinct (source_kind,coalesce(source_locator,source_url))) from echo_source_captures where id=any(p_capture_ids) and gym_id=p_gym and echo_account_key=b.echo_account_key)<>cardinality(p_capture_ids) then raise exception 'duplicate_source_identity'; end if;
  old:=b.snapshot_bytes::jsonb;
  if (select count(distinct (x->>'source_kind',coalesce(x->>'source_locator',x->>'source_url'))) from jsonb_array_elements(old->'captures') x)<>jsonb_array_length(old->'captures') then raise exception 'duplicate_source_identity'; end if;
  for c in select value from jsonb_array_elements(old->'captures') loop
    select * into current_capture from echo_source_captures where id=any(p_capture_ids) and gym_id=p_gym and source_kind=c->>'source_kind' and coalesce(source_locator,source_url)=coalesce(c->>'source_locator',c->>'source_url');
    if current_capture.id is null or current_capture.echo_account_key<>b.echo_account_key or current_capture.provider_account_id is distinct from c->>'provider_account_id' or current_capture.mapping_revision<>c->>'mapping_revision' or current_capture.mapping_evidence<>c->'mapping_evidence' or current_capture.capture_provider<>c->>'capture_provider' or current_capture.fetched_at<clock_timestamp()-interval '7 days' or current_capture.fetched_at>clock_timestamp() then raise exception 'identity_or_freshness_changed'; end if;
    if exists(select 1 from echo_source_captures n where n.gym_id=p_gym and n.source_kind=current_capture.source_kind and coalesce(n.source_locator,n.source_url)=coalesce(current_capture.source_locator,current_capture.source_url) and n.captured_at>current_capture.captured_at) then raise exception 'stale_source_capture'; end if;
  end loop;
  select * into pal from echo_source_captures where id=p_palette_capture and id=any(p_capture_ids) and gym_id=p_gym and echo_account_key=b.echo_account_key and source_kind in ('website','website_asset');
  if pal.id is null or p_primary_offset is null or p_secondary_offset is null or p_primary_offset<0 or p_secondary_offset<0 or coalesce(pal.source_locator,pal.source_url) is distinct from (select coalesce(x->>'source_locator',x->>'source_url') from jsonb_array_elements(old->'captures') x where x->>'id'=old->'palette'->>'capture_id') then raise exception 'selected_palette_changed'; end if;
  if p_primary_offset::bigint+7>octet_length(pal.raw_bytes) or p_secondary_offset::bigint+7>octet_length(pal.raw_bytes) then raise exception 'selected_palette_changed'; end if;
  begin
    primary_color:=convert_from(substring(pal.raw_bytes from p_primary_offset+1 for 7),'UTF8');
    secondary_color:=convert_from(substring(pal.raw_bytes from p_secondary_offset+1 for 7),'UTF8');
  exception when character_not_in_repertoire then
    raise exception 'selected_palette_changed';
  end;
  if primary_color is distinct from old->'palette'->>'primary' or secondary_color is distinct from old->'palette'->>'secondary' then raise exception 'selected_palette_changed'; end if;
  facts:=echo_source_brand_fact_spans(p_gym,b.echo_account_key,p_capture_ids,p_fact_spans);
  if (select coalesce(jsonb_agg(jsonb_build_object('key',x->>'key','text',x->>'text','source_locator',x->>'source_locator') order by x->>'key'),'[]'::jsonb) from jsonb_array_elements(facts) x) is distinct from (select coalesce(jsonb_agg(jsonb_build_object('key',x->>'key','text',x->>'text','source_locator',x->>'source_locator') order by x->>'key'),'[]'::jsonb) from jsonb_array_elements(old->'selected_facts') x) then raise exception 'selected_facts_changed'; end if;
  select jsonb_agg(jsonb_build_object('id',id,'gym_id',gym_id,'echo_account_key',echo_account_key,'source_kind',source_kind,'source_url',source_url,'source_locator',source_locator,'capture_provider',capture_provider,'provider_response_id',provider_response_id,'provider_account_id',provider_account_id,'source_revision',source_revision,'mapping_revision',mapping_revision,'mapping_evidence',mapping_evidence,'fetched_at',fetched_at,'bytes_sha256',bytes_sha256,'bytes_base64',replace(encode(raw_bytes,'base64'),chr(10),'')) order by id) into snapshots from echo_source_captures where id=any(p_capture_ids) and gym_id=p_gym and echo_account_key=b.echo_account_key;
  frozen:=jsonb_build_object('schema_version',1,'gym_id',p_gym,'echo_account_key',b.echo_account_key,'fact_policy','delegated_supported_facts','selected_facts',facts,'captures',snapshots,'palette',jsonb_build_object('capture_id',pal.id,'bytes_sha256',pal.bytes_sha256,'primary',old->'palette'->>'primary','secondary',old->'palette'->>'secondary','primary_byte_offset',p_primary_offset,'secondary_byte_offset',p_secondary_offset));
  if b.schema_version=2 then frozen:=frozen||jsonb_build_object('schema_version',2,'source_policy',old->'source_policy','provider_status_receipt',echo_source_brand_provider_receipt(p_gym,b.echo_account_key)); end if;
  insert into echo_source_brand_observations(gym_id,bundle_id,configuration_sha256,snapshot_bytes,validator_revision,validation_report)
  values(p_gym,b.id,b.content_sha256,frozen::text,p_validator_revision,p_validation_report) returning * into result;
  return result;
end $$;
revoke all on function public.echo_source_brand_revalidate(uuid,uuid,text,uuid[],uuid,integer,integer,jsonb,text,jsonb) from public,anon,authenticated;
grant execute on function public.echo_source_brand_revalidate(uuid,uuid,text,uuid[],uuid,integer,integer,jsonb,text,jsonb) to service_role;

-- Explicit delegation requires the same authenticated Blake identity; no broad staff role.
create or replace function public.echo_source_brand_grant(p_gym uuid,p_actor text,p_subject text,p_action text,p_request uuid)
returns public.echo_source_brand_capability_events language plpgsql security definer set search_path=public,pg_temp as $$
declare e public.echo_source_brand_capability_events;
begin
  if public.echo_source_brand_authority(p_actor,p_gym)<>'blake' then raise exception 'blake_required'; end if;
  if p_action is null or p_action not in ('grant','revoke') or not exists(select 1 from app_users where clerk_user_id=p_subject and role in ('owner','executive','admin_no_financials')) then raise exception 'invalid_capability_subject'; end if;
  perform pg_advisory_xact_lock(hashtextextended(p_gym::text,0));
  select * into e from public.echo_source_brand_capability_events where request_id=p_request;
  if e.id is not null then
    if e.gym_id=p_gym and e.granted_by=p_actor and e.clerk_user_id=p_subject and e.action=p_action then return e; end if;
    raise exception 'request_id_conflict';
  end if;
  insert into public.echo_source_brand_capability_events(gym_id,clerk_user_id,action,granted_by,request_id) values(p_gym,p_subject,p_action,p_actor,p_request) returning * into e;
  return e;
end $$;
revoke all on function public.echo_source_brand_grant(uuid,text,text,text,uuid) from public,anon,authenticated;
grant execute on function public.echo_source_brand_grant(uuid,text,text,text,uuid) to service_role;
grant select on public.echo_source_brand_provider_status,public.echo_source_captures,public.echo_source_brand_capability_events,public.echo_source_brand_bundles,public.echo_source_brand_receipts,public.echo_source_brand_observations to service_role;
grant insert on public.echo_source_captures to service_role;
revoke insert,update,delete on public.echo_source_brand_provider_status,public.echo_source_brand_capability_events,public.echo_source_brand_bundles,public.echo_source_brand_receipts,public.echo_source_brand_observations from service_role;
