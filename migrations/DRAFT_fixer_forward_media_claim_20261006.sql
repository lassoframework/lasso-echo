-- DRAFT / UNAPPLIED / DEFAULT OFF. Task fixer-forward-media-claim-20261006.
-- Do not apply production DDL or enable this draft. Requires #306/#307 draft
-- schema and owner-attested source/rendition receipt helpers. No publisher is
-- wired here. An owner must review historical unknown-source policy and wire
-- EVERY provider path to this claim before any send; only then may a separately
-- reviewed activation set enabled. No service caller can enable this table.
-- Forward occupancy is independent of legacy/global/pHash history; this file
-- neither imports nor clears history and does not replace their holds.
-- Rollback before use: drop the RPC and new tables. After any real claim,
-- preserve the append-only occupancy/receipt tables permanently.
begin;

create table public.fixer_forward_media_claim_gate_20261006 (
  tenant_id text primary key,
  enabled boolean not null default false
);
create table public.fixer_forward_media_use_20261006 (
  tenant_id text not null,
  fingerprint text not null check (fingerprint ~ '^md5:[0-9a-f]{32}$'),
  post_date date not null,
  group_key text not null check (btrim(group_key) <> ''),
  first_calendar_row_id uuid not null,
  first_claim_token uuid not null,
  claimed_at timestamptz not null default now(),
  primary key (tenant_id, fingerprint)
);
create table public.fixer_forward_media_claim_receipt_20261006 (
  claim_token uuid primary key,
  calendar_row_id uuid not null,
  tenant_id text not null,
  post_date date not null,
  group_key text not null,
  fingerprints text[] not null check (cardinality(fingerprints)>0),
  source_url text not null,
  image_url text not null,
  thumbnail_url text,
  claimed_at timestamptz not null default now()
);

-- Reuse the draft's immutable UPDATE/DELETE guard. TRUNCATE is revoked too.
create trigger fixer_forward_media_use_immutable_20261006 before update or delete
  on public.fixer_forward_media_use_20261006 for each row
  execute function public.visual_global_immutable();
create trigger fixer_forward_media_receipt_immutable_20261006 before update or delete
  on public.fixer_forward_media_claim_receipt_20261006 for each row
  execute function public.visual_global_immutable();

alter table public.fixer_forward_media_claim_gate_20261006 enable row level security;
alter table public.fixer_forward_media_use_20261006 enable row level security;
alter table public.fixer_forward_media_claim_receipt_20261006 enable row level security;
revoke all on public.fixer_forward_media_claim_gate_20261006,
  public.fixer_forward_media_use_20261006,
  public.fixer_forward_media_claim_receipt_20261006
  from public, anon, authenticated, service_role;
grant select on public.fixer_forward_media_use_20261006,
  public.fixer_forward_media_claim_receipt_20261006 to service_role;
create policy fixer_forward_media_use_read_20261006
  on public.fixer_forward_media_use_20261006 for select to service_role using (true);
create policy fixer_forward_media_receipt_read_20261006
  on public.fixer_forward_media_claim_receipt_20261006 for select to service_role using (true);

-- Auth boundary: executable only by the authenticated backend service role.
-- Tenant/date/group/hash are NEVER supplied by the caller. Token must equal
-- the existing server-minted owned publish token on the locked calendar row.
-- This does not authorize browser JWT users, provider activation or sends.
-- Caller must treat any exception as a no-send outcome. Errors are deliberately
-- not swallowed: missing ledger/schema/receipt dependencies fail closed.
create function public.fixer_claim_forward_media_20261006(
  p_calendar_row_id uuid, p_claim_token uuid
) returns boolean language plpgsql security definer
set search_path = pg_catalog, public as $$
declare
  r public.content_calendar%rowtype;
  tenant text;
  hashes text[];
  fp text;
  receipt public.fixer_forward_media_claim_receipt_20261006%rowtype;
  occupied public.fixer_forward_media_use_20261006%rowtype;
begin
  if p_calendar_row_id is null or p_claim_token is null then
    raise exception 'existing owned calendar claim required' using errcode='22023';
  end if;
  select * into r from public.content_calendar where id=p_calendar_row_id for update;
  if not found or r.publish_claim_token is distinct from p_claim_token
      or r.status not in ('publishing','published') or r.status is null
      or r.variant_status is distinct from 'active'
      or r.post_date is null or r.publish_reservation_day is null
      or r.publish_reservation_day is distinct from r.post_date
      or r.media_not_ready_reason is not null
      or nullif(btrim(r.visual_group_key),'') is null then
    raise exception 'calendar claim ownership or ready media invalid' using errcode='23514';
  end if;
  tenant:=public.visual_group_tenant_strict(r.gym_id)::text;
  if not exists(select 1 from public.fixer_forward_media_claim_gate_20261006 g
      where g.tenant_id=tenant and g.enabled) then
    raise exception 'forward media guard is OFF pending history and publisher review' using errcode='55000';
  end if;
  hashes:=public.visual_global_row_verified_fingerprints(r,r.visual_group_key);
  if hashes is null or cardinality(hashes)=0 or exists(
      select 1 from unnest(hashes) h where h is null or h !~ '^md5:[0-9a-f]{32}$') then
    raise exception 'original source and delivered bytes must be owner attested' using errcode='23514';
  end if;

  -- Sorted transaction-scoped byte locks serialize competing rows without
  -- holding fleet/other tenant locks. A unique PK is the final authority.
  -- Include token lock to serialize fabricated token reuse across row IDs.
  perform pg_advisory_xact_lock(hashtextextended(
    jsonb_build_array('fixer_forward_token_20261006',p_claim_token)::text,0));
  foreach fp in array hashes loop
    perform pg_advisory_xact_lock(hashtextextended(
      jsonb_build_array('fixer_forward_byte_20261006',tenant,fp)::text,0));
  end loop;
  select * into receipt from public.fixer_forward_media_claim_receipt_20261006
    where claim_token=p_claim_token;
  if found then
    if receipt.calendar_row_id is distinct from r.id
      or receipt.tenant_id is distinct from tenant
      or receipt.post_date is distinct from r.post_date
      or receipt.group_key is distinct from r.visual_group_key
      or receipt.fingerprints is distinct from hashes
      or receipt.source_url is distinct from r.source_media_url
      or receipt.image_url is distinct from r.image_url
      or receipt.thumbnail_url is distinct from r.thumbnail_url then
      raise exception 'claim token receipt differs from persisted media' using errcode='23514';
    end if;
    foreach fp in array hashes loop
      select * into occupied from public.fixer_forward_media_use_20261006
        where tenant_id=tenant and fingerprint=fp;
      if not found or occupied.post_date is distinct from r.post_date
          or occupied.group_key is distinct from r.visual_group_key then
        raise exception 'claim receipt occupancy unavailable or inconsistent' using errcode='23514';
      end if;
    end loop;
    return true;
  end if;
  -- A first claim after provider delivery is unknown history, never a new use.
  if r.status <> 'publishing' or r.published_at is not null or r.late_post_id is not null then
    raise exception 'new forward claim requires an unsent publishing row' using errcode='23514';
  end if;
  foreach fp in array hashes loop
    insert into public.fixer_forward_media_use_20261006
      (tenant_id,fingerprint,post_date,group_key,first_calendar_row_id,first_claim_token)
      values(tenant,fp,r.post_date,r.visual_group_key,r.id,p_claim_token)
      on conflict (tenant_id,fingerprint) do nothing;
    select * into occupied from public.fixer_forward_media_use_20261006
      where tenant_id=tenant and fingerprint=fp;
    if not found or occupied.post_date is distinct from r.post_date
        or occupied.group_key is distinct from r.visual_group_key then
      -- Raising rolls back the entire multi-byte set and any prior inserts.
      raise exception 'source or rendition already consumed by another date/group'
        using errcode='23514';
    end if;
  end loop;
  insert into public.fixer_forward_media_claim_receipt_20261006
    (claim_token,calendar_row_id,tenant_id,post_date,group_key,fingerprints,
     source_url,image_url,thumbnail_url)
    values(p_claim_token,r.id,tenant,r.post_date,r.visual_group_key,hashes,
      r.source_media_url,r.image_url,r.thumbnail_url);
  return true;
end;
$$;
revoke all on function public.fixer_claim_forward_media_20261006(uuid,uuid)
  from public, anon, authenticated, service_role;
grant execute on function public.fixer_claim_forward_media_20261006(uuid,uuid) to service_role;
commit;
