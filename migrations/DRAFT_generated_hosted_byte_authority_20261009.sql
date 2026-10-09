-- DRAFT / UNAPPLIED / UNWIRED. Apply only AFTER the reviewed portal 0625 stack.
-- Does not remove Echo's unconditional generated hold or change calendar rows.
-- SQL cannot fetch hosted objects: only a separately provisioned trusted issuer
-- may submit independently GET-read bytes. Producers/service_role cannot issue.
begin;
do $$
begin
 if current_user <> 'postgres' or to_regclass('public.calendar_generated_artifact_versions') is null
  or not exists(select 1 from pg_trigger where tgrelid='public.calendar_generated_artifact_versions'::regclass
   and tgname='generated_version_immutable' and tgenabled='O')
  or not exists(select 1 from pg_trigger where tgrelid='public.calendar_generated_artifact_versions'::regclass
   and tgname='generated_version_no_truncate' and tgenabled='O') then
  raise exception 'reviewed immutable portal generated version authority required' using errcode='23514'; end if;
end $$;
do $$ begin
 if not exists(select 1 from pg_roles where rolname='generated_hosted_byte_issuer_20261009') then
  create role generated_hosted_byte_issuer_20261009 nologin nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
 end if;
 if not exists(select 1 from pg_roles where rolname='generated_hosted_byte_reader_20261009') then
  create role generated_hosted_byte_reader_20261009 nologin nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
 end if;
 if exists(select 1 from pg_roles where rolname in ('generated_hosted_byte_issuer_20261009','generated_hosted_byte_reader_20261009')
  and (rolcanlogin or rolsuper or rolcreatedb or rolcreaterole or rolbypassrls or rolinherit)) then
  raise exception 'dedicated authority roles must be unprivileged NOLOGIN' using errcode='23514'; end if;
 if exists(select 1 from pg_roles p where p.rolname in ('anon','authenticated','service_role') and
  (pg_has_role(p.rolname,'generated_hosted_byte_issuer_20261009','MEMBER')
   or pg_has_role(p.rolname,'generated_hosted_byte_reader_20261009','MEMBER'))) then
  raise exception 'producer roles cannot inherit dedicated authority' using errcode='23514'; end if;
end $$;
grant usage on schema public to generated_hosted_byte_issuer_20261009,generated_hosted_byte_reader_20261009;

-- Admin-only configuration. Grants use authenticated session_user, never a
-- caller argument, JWT claim, alias, SET ROLE or application grant dictionary.
-- Provision a distinct nonsuperuser LOGIN per issuer/reader and tenant grants.
create table public.generated_hosted_byte_principals_20261009 (
 principal name not null, gym_id text not null check(btrim(gym_id)<>'' and length(gym_id)<=128),
 can_issue boolean not null default false, can_lookup boolean not null default false,
 primary key(principal,gym_id), check(can_issue or can_lookup)
);
create table public.generated_hosted_byte_receipts_20261009 (
 receipt_id uuid primary key,
 artifact_version_id uuid not null unique references public.calendar_generated_artifact_versions(id),
 gym_id text not null, hosted_url text not null unique,
 delivered_sha256 text not null check(delivered_sha256 ~ '^[0-9a-f]{64}$'),
 manifest_sha256 text not null check(manifest_sha256 ~ '^[0-9a-f]{64}$'),
 manifest_bytes bytea not null check(octet_length(manifest_bytes) between 1 and 65536),
 delivered_length integer not null check(delivered_length between 1 and 8388608),
 issued_by name not null, verified_at timestamptz not null default clock_timestamp()
);
alter table public.generated_hosted_byte_principals_20261009 enable row level security;
alter table public.generated_hosted_byte_receipts_20261009 enable row level security;
revoke all on public.generated_hosted_byte_principals_20261009,public.generated_hosted_byte_receipts_20261009,
 public.calendar_generated_artifact_versions from public,anon,authenticated,service_role,
 generated_hosted_byte_issuer_20261009,generated_hosted_byte_reader_20261009;
-- Preserve the reviewed portal's read-only service-role version access.
grant select on public.calendar_generated_artifact_versions to service_role;

create function public.generated_hosted_byte_immutable_20261009() returns trigger
language plpgsql set search_path=pg_catalog,public as $$
begin raise exception 'hosted byte receipts are immutable' using errcode='23514'; end $$;
create trigger generated_hosted_byte_immutable before update or delete on public.generated_hosted_byte_receipts_20261009
 for each row execute function public.generated_hosted_byte_immutable_20261009();
create trigger generated_hosted_byte_no_truncate before truncate on public.generated_hosted_byte_receipts_20261009
 for each statement execute function public.generated_hosted_byte_immutable_20261009();

create function public.generated_hosted_byte_authorized_20261009(p_gym text,p_purpose text) returns boolean
language sql stable security definer set search_path=pg_catalog,public as $$
 select coalesce(exists(select 1 from public.generated_hosted_byte_principals_20261009 g
  where g.principal=session_user and g.gym_id=p_gym and (
   (p_purpose='issue' and g.can_issue and pg_has_role(session_user,'generated_hosted_byte_issuer_20261009','MEMBER'))
   or (p_purpose='lookup' and (g.can_lookup or g.can_issue) and
    (pg_has_role(session_user,'generated_hosted_byte_reader_20261009','MEMBER')
     or pg_has_role(session_user,'generated_hosted_byte_issuer_20261009','MEMBER'))))),false);
$$;

create function public.generated_hosted_byte_issue_20261009(
 p_gym text,p_version uuid,p_url text,p_expected_sha text,p_manifest_sha text,p_hosted_bytes bytea,p_manifest_bytes bytea)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare m jsonb; receipt jsonb; existing public.generated_hosted_byte_receipts_20261009%rowtype; allowed boolean;
 v public.calendar_generated_artifact_versions%rowtype; rid uuid;
begin
 if not public.generated_hosted_byte_authorized_20261009(p_gym,'issue') then
  raise exception 'dedicated tenant issuer required' using errcode='42501'; end if;
 -- Serialize principal-grant revocation with the whole issuance transaction.
 select can_issue into allowed from public.generated_hosted_byte_principals_20261009
  where principal=session_user and gym_id=p_gym for share;
 if not coalesce(allowed,false) then
  raise exception 'dedicated tenant issuer required' using errcode='42501'; end if;
 if p_version is null or p_url is null or length(p_url)>2048
  -- Match the reader's single canonical origin spelling. Hostname case,
  -- explicit :443 and trailing-dot aliases cannot evade exact URL uniqueness.
  or p_url !~ '^https://([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?/[^[:space:]?#]*$'
  or p_expected_sha is null or p_expected_sha !~ '^[0-9a-f]{64}$'
  or p_manifest_sha is null or p_manifest_sha !~ '^[0-9a-f]{64}$'
  or p_hosted_bytes is null or octet_length(p_hosted_bytes) not between 1 and 8388608
  or p_manifest_bytes is null or octet_length(p_manifest_bytes) not between 1 and 65536
  or encode(sha256(p_hosted_bytes),'hex') is distinct from p_expected_sha
  or encode(sha256(p_manifest_bytes),'hex') is distinct from p_manifest_sha then
  raise exception 'exact hosted bytes and manifest digest required' using errcode='23514'; end if;
 m:=convert_from(p_manifest_bytes,'UTF8')::jsonb;
 if jsonb_typeof(m) is distinct from 'object' or m->>'gym_id' is distinct from p_gym
  or m->>'artifact_version_id' is distinct from p_version::text
  or m->>'hosted_url' is distinct from p_url or m->>'delivered_sha256' is distinct from p_expected_sha then
  raise exception 'manifest identity binding required' using errcode='23514'; end if;
 -- Serialize UUID replay contenders without touching any calendar/claim lock.
 perform pg_advisory_xact_lock(hashtextextended(p_version::text,20261009));
 select * into existing from public.generated_hosted_byte_receipts_20261009 where artifact_version_id=p_version;
 if found then
  if row(existing.gym_id,existing.hosted_url,existing.delivered_sha256,existing.manifest_sha256,existing.manifest_bytes,existing.delivered_length)
   is distinct from row(p_gym,p_url,p_expected_sha,p_manifest_sha,p_manifest_bytes,octet_length(p_hosted_bytes)) then
   raise exception 'immutable generated version replay conflict' using errcode='23514'; end if;
  select * into v from public.calendar_generated_artifact_versions where id=p_version;
  if not found or v.delivery_receipt->>'receipt_id' is distinct from existing.receipt_id::text
   or row(v.gym_id,v.image_url,v.delivered_sha256,v.render_manifest_digest)
    is distinct from row(p_gym,p_url,p_expected_sha,p_manifest_sha)
   or v.delivery_receipt is distinct from jsonb_build_object('receipt_id',existing.receipt_id,
    'gym_id',p_gym,'artifact_version_id',p_version,'hosted_url',p_url,
    'delivered_sha256',p_expected_sha,'render_manifest',m) then
   raise exception 'committed authority receipt invalid' using errcode='23514'; end if;
  return v.delivery_receipt;
 end if;
 if exists(select 1 from public.calendar_generated_artifact_versions where id=p_version) then
  raise exception 'unissued existing version cannot become authority' using errcode='23514'; end if;
 if exists(select 1 from public.generated_hosted_byte_receipts_20261009 where hosted_url=p_url) then
  raise exception 'hosted URL belongs to another immutable version' using errcode='23514'; end if;
 rid:=gen_random_uuid();
 receipt:=jsonb_build_object('receipt_id',rid,'gym_id',p_gym,'artifact_version_id',p_version,
  'hosted_url',p_url,'delivered_sha256',p_expected_sha,'render_manifest',m);
 insert into public.calendar_generated_artifact_versions(id,gym_id,image_url,delivered_sha256,render_manifest_digest,delivery_receipt)
 values(p_version,p_gym,p_url,p_expected_sha,p_manifest_sha,receipt);
 insert into public.generated_hosted_byte_receipts_20261009(receipt_id,artifact_version_id,gym_id,hosted_url,delivered_sha256,
  manifest_sha256,manifest_bytes,delivered_length,issued_by)
 values(rid,p_version,p_gym,p_url,p_expected_sha,p_manifest_sha,p_manifest_bytes,octet_length(p_hosted_bytes),session_user);
 return receipt;
end $$;

create function public.generated_hosted_byte_lookup_20261009(
 p_gym text,p_version uuid,p_url text,p_sha text,p_manifest_sha text,p_receipt uuid)
returns jsonb language plpgsql stable security definer set search_path=pg_catalog,public as $$
declare result jsonb;
begin
 if not public.generated_hosted_byte_authorized_20261009(p_gym,'lookup') then
  raise exception 'dedicated tenant reader required' using errcode='42501'; end if;
 select v.delivery_receipt into result from public.generated_hosted_byte_receipts_20261009 r
  join public.calendar_generated_artifact_versions v on v.id=r.artifact_version_id
  where r.gym_id=p_gym and r.artifact_version_id=p_version and r.hosted_url=p_url
   and r.delivered_sha256=p_sha and r.manifest_sha256=p_manifest_sha and r.receipt_id=p_receipt
   and row(v.gym_id,v.image_url,v.delivered_sha256,v.render_manifest_digest)
    is not distinct from row(r.gym_id,r.hosted_url,r.delivered_sha256,r.manifest_sha256)
   and v.delivery_receipt=jsonb_build_object('receipt_id',r.receipt_id,'gym_id',r.gym_id,
    'artifact_version_id',r.artifact_version_id,'hosted_url',r.hosted_url,'delivered_sha256',r.delivered_sha256,
    'render_manifest',convert_from(r.manifest_bytes,'UTF8')::jsonb);
 return result;
end $$;

revoke all on function public.generated_hosted_byte_immutable_20261009(),
 public.generated_hosted_byte_authorized_20261009(text,text),
 public.generated_hosted_byte_issue_20261009(text,uuid,text,text,text,bytea,bytea),
 public.generated_hosted_byte_lookup_20261009(text,uuid,text,text,text,uuid)
 from public,anon,authenticated,service_role,generated_hosted_byte_issuer_20261009,generated_hosted_byte_reader_20261009;
grant execute on function public.generated_hosted_byte_authorized_20261009(text,text),
 public.generated_hosted_byte_lookup_20261009(text,uuid,text,text,text,uuid)
 to generated_hosted_byte_issuer_20261009,generated_hosted_byte_reader_20261009;
grant execute on function public.generated_hosted_byte_issue_20261009(text,uuid,text,text,text,bytea,bytea)
 to generated_hosted_byte_issuer_20261009;
commit;
