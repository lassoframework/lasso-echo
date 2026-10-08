-- DRAFT / UNAPPLIED / DEFAULT OFF. No production activation is authorized.
-- Self-contained authority: only content_calendar and its existing owned publish
-- token/reservation columns are prerequisites; NO #306/#307 draft helpers.
-- Narrow separately provisioned trusted attester role records independently
-- checked source/delivered byte evidence through the immutable attestation RPC.
-- Optional aliases are owner-only; new gyms use their persisted gym_id directly.
-- A service caller cannot attest its own hashes, write occupancy, or enable gates.
-- EVERY provider boundary must call the RPC immediately before sending, require
-- literal true and hold on any exception. Selection reads are advisory only.
-- Fleet-wide fingerprint occupancy never expires or releases on calendar delete.
-- post_date identifies the content group; reservation_day identifies the actual
-- owned send slot and may differ for catch-up. Same tenant/group/content date
-- siblings may reuse bytes; another gym or content date may never reuse them.
-- No historical import, no bypass of legacy/global/pHash or other publish holds.
-- Rollback before use: remove new objects. After use preserve occupancy/receipts.
-- Authority lock waits fail closed after 5s per acquisition, including RPC
-- callers without a connection deadline. Dedicated owner/attester connections
-- also set lock/statement deadlines BEFORE commands: a trigger's SET is restored
-- on return and cannot bound a later wait in the outer INSERT. Setting
-- statement_timeout inside a function does not bound its invoking statement.
begin;

-- Standalone additive provenance fields. Existing rows stay NULL/held until
-- trusted preparation establishes original and stable logical sibling group.
alter table public.content_calendar add column if not exists source_media_url text;
alter table public.content_calendar add column if not exists visual_group_key text;
alter table public.content_calendar add column if not exists source_media_asset_id text;
alter table public.content_calendar add column if not exists render_manifest_digest text;

-- No login, password, membership or provider activation is provisioned here.
create role fixer_forward_media_attester_20261006 nologin;
grant usage on schema public to fixer_forward_media_attester_20261006;
-- A separately provisioned LOGIN may inherit this narrow owner role. The
-- service/attester roles cannot prepare originals, clear history or bind renders.
create role fixer_forward_media_owner_20261006 nologin;
grant usage on schema public to fixer_forward_media_owner_20261006;

create function public.fixer_forward_media_immutable_20261006()
returns trigger language plpgsql set search_path=pg_catalog,public as $$
begin
  raise exception 'forward media authority is immutable' using errcode='23514';
end;
$$;

revoke all on function public.fixer_forward_media_immutable_20261006() from public,anon,authenticated,service_role;

create table public.fixer_forward_media_tenant_alias_20261006 (
  alias_key text primary key check (alias_key=btrim(alias_key) and alias_key<>''),
  tenant_id text not null check (tenant_id=btrim(tenant_id) and tenant_id<>'')
);
create table public.fixer_forward_media_claim_gate_20261006 (
  tenant_id text primary key,
  enabled boolean not null default false
);
-- Owner-prepared source authority, distinct from arbitrary fetched URLs. Only a
-- verified original registry may establish a source identity; an attester has
-- no insert privilege here. Versioned render manifests bind every output to
-- that original. All preparation/import remains separately authorized/offline.
create table public.fixer_forward_media_original_registry_20261006 (
  tenant_id text not null check (tenant_id=btrim(tenant_id) and tenant_id<>''),
  source_asset_id text not null check (source_asset_id=btrim(source_asset_id) and source_asset_id<>''),
  source_url text not null check (source_url ~ '^https://[^[:space:]]+$'),
  source_fingerprint text not null check (source_fingerprint ~ '^md5:[0-9a-f]{32}$'),
  source_length bigint not null check (source_length>0 and source_length<=134217728),
  registry_evidence_ref text not null check (btrim(registry_evidence_ref)<>''),
  registered_at timestamptz not null default now(),
  primary key(tenant_id,source_asset_id),
  unique(tenant_id,source_url)
);
-- Direct OWNER-ONLY preparation protocol for a future offline importer:
-- BEGIN; insert verified original registry tuple; insert clearance using the
-- exact tuple and independently audited fleet historical evidence; insert
-- versioned render manifest; COMMIT. No registry/upload/generation automatically
-- receives clearance. Generated originals use an evidence-backed generation
-- asset identity and registry evidence reference, subject to the same historical
-- byte check. Uncertain/used decisions permanently quarantine those known bytes;
-- missing clearance holds. Do not use service_role or the attester to prepare.
create table public.fixer_forward_media_history_clearance_20261006 (
  tenant_id text not null,
  source_asset_id text not null,
  source_url text not null check (source_url ~ '^https://[^[:space:]]+$'),
  source_fingerprint text not null check (source_fingerprint ~ '^md5:[0-9a-f]{32}$'),
  source_length bigint not null check (source_length>0 and source_length<=134217728),
  registry_evidence_ref text not null check (btrim(registry_evidence_ref)<>''),
  decision text not null check (decision in ('cleared_unused','hold_uncertain','hold_used')),
  history_evidence_ref text not null check (btrim(history_evidence_ref)<>''),
  checked_at timestamptz not null default now(),
  primary key(tenant_id,source_asset_id),
  foreign key(tenant_id,source_asset_id) references public.fixer_forward_media_original_registry_20261006(tenant_id,source_asset_id)
);
create index fixer_forward_history_fingerprint_20261006
  on public.fixer_forward_media_history_clearance_20261006(source_fingerprint);
-- Historical authority appends serialize with attestations/claims under the
-- existing graph lock, so a waiting claim observes committed quarantine data.
create function public.fixer_forward_history_lock_20261006()
returns trigger language plpgsql set search_path=pg_catalog,public
set lock_timeout='5s' as $$
begin
  if current_setting('transaction_isolation')<>'read committed' then
    raise exception 'forward media authority requires read committed isolation' using errcode='25000';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
  return new;
end;
$$;
revoke all on function public.fixer_forward_history_lock_20261006()
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006;
create trigger history_graph_lock before insert on public.fixer_forward_media_history_clearance_20261006
  for each row execute function public.fixer_forward_history_lock_20261006();
create table public.fixer_forward_media_render_manifest_20261006 (
  manifest_digest text primary key check (manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
  tenant_id text not null,
  source_asset_id text not null,
  image_url text not null check (image_url ~ '^https://[^[:space:]]+$'),
  image_fingerprint text not null check (image_fingerprint ~ '^md5:[0-9a-f]{32}$'),
  image_length bigint not null check (image_length>0 and image_length<=134217728),
  thumbnail_url text check (thumbnail_url ~ '^https://[^[:space:]]+$'),
  thumbnail_fingerprint text check (thumbnail_fingerprint ~ '^md5:[0-9a-f]{32}$'),
  thumbnail_length bigint check (thumbnail_length>0 and thumbnail_length<=134217728),
  operation text not null check (operation in ('same_object','render','reburn','rehost')),
  -- Versioned replay inputs are owner-prepared. Missing inputs remain an
  -- attester hold; a URL or caption supplied by the publisher is never proof.
  render_recipe jsonb,
  render_evidence_ref text not null check (btrim(render_evidence_ref)<>''),
  registered_at timestamptz not null default now(),
  foreign key(tenant_id,source_asset_id) references public.fixer_forward_media_original_registry_20261006(tenant_id,source_asset_id),
  check ((thumbnail_url is null and thumbnail_fingerprint is null and thumbnail_length is null)
    or (thumbnail_url is not null and thumbnail_fingerprint is not null and thumbnail_length is not null))
);
-- A new manifest for already registered media must serialize with lookup and
-- attestation too. Otherwise a binder could observe one match while an owner
-- appends a second manifest for the same media before that bind commits.
create trigger render_manifest_graph_lock before insert on public.fixer_forward_media_render_manifest_20261006
  for each row execute function public.fixer_forward_history_lock_20261006();
-- The independently credentialed attester fetches exact objects and records bytes.
-- An immutable URL cannot later be rebound to different bytes. Source hashes are
-- the hashes of original bytes, never guessed from a derivative/URL/asset ID.
create table public.fixer_forward_media_object_read_20261006 (
  receipt_id uuid primary key,
  tenant_id text not null,
  exact_url text not null check (exact_url ~ '^https://[^[:space:]]+$'),
  fingerprint text not null check (fingerprint ~ '^md5:[0-9a-f]{32}$'),
  byte_length bigint not null check (byte_length>0),
  evidence_ref text not null check (btrim(evidence_ref)<>''),
  verified_by text not null check (btrim(verified_by)<>''),
  verified_at timestamptz not null default now(),
  unique(tenant_id,exact_url)
);
-- Explicit trusted binding of the original source to every delivered object.
-- A transformed image/thumbnail requires trusted-attester verified render lineage evidence.
create table public.fixer_forward_media_lineage_20261006 (
  evidence_id uuid primary key,
  calendar_row_id uuid not null,
  row_revision text not null,
  operation text not null check (operation in ('same_object','render','reburn','rehost')),
  tenant_id text not null,
  group_key text not null check (btrim(group_key)<>''),
  source_read_receipt uuid not null references public.fixer_forward_media_object_read_20261006(receipt_id),
  image_read_receipt uuid not null references public.fixer_forward_media_object_read_20261006(receipt_id),
  thumbnail_read_receipt uuid references public.fixer_forward_media_object_read_20261006(receipt_id),
  source_asset_id text not null,
  manifest_digest text not null references public.fixer_forward_media_render_manifest_20261006(manifest_digest),
  render_evidence_ref text,
  verified_by text not null check (btrim(verified_by)<>''),
  verified_at timestamptz not null default now(),
  unique(calendar_row_id,row_revision)
);
create index fixer_forward_read_fingerprint_20261006
  on public.fixer_forward_media_object_read_20261006(fingerprint);
create index fixer_forward_lineage_image_20261006
  on public.fixer_forward_media_lineage_20261006(image_read_receipt);
create index fixer_forward_lineage_thumbnail_20261006
  on public.fixer_forward_media_lineage_20261006(thumbnail_read_receipt)
  where thumbnail_read_receipt is not null;
create table public.fixer_forward_media_use_20261006 (
  fingerprint text primary key check (fingerprint ~ '^md5:[0-9a-f]{32}$'),
  tenant_id text not null,
  post_date date not null,
  group_key text not null check (btrim(group_key)<>''),
  first_calendar_row_id uuid not null,
  first_claim_token uuid not null,
  claimed_at timestamptz not null default now()
);
create table public.fixer_forward_media_claim_receipt_20261006 (
  claim_token uuid primary key,
  calendar_row_id uuid not null,
  tenant_id text not null,
  post_date date not null,
  reservation_day date not null,
  group_key text not null,
  evidence_id uuid not null references public.fixer_forward_media_lineage_20261006(evidence_id),
  fingerprints text[] not null check (cardinality(fingerprints)>0),
  source_url text not null,
  image_url text not null,
  thumbnail_url text,
  claimed_at timestamptz not null default now()
);

-- Both row mutation and TRUNCATE are forbidden, including owner accidents.
do $$
declare t text;
begin
  foreach t in array array['fixer_forward_media_tenant_alias_20261006',
    'fixer_forward_media_original_registry_20261006','fixer_forward_media_render_manifest_20261006',
    'fixer_forward_media_history_clearance_20261006',
    'fixer_forward_media_object_read_20261006','fixer_forward_media_lineage_20261006',
    'fixer_forward_media_use_20261006','fixer_forward_media_claim_receipt_20261006'] loop
    execute format('create trigger immutable_row before update or delete on public.%I for each row execute function public.fixer_forward_media_immutable_20261006()',t);
    execute format('create trigger immutable_truncate before truncate on public.%I for each statement execute function public.fixer_forward_media_immutable_20261006()',t);
  end loop;
  foreach t in array array['fixer_forward_media_tenant_alias_20261006',
    'fixer_forward_media_claim_gate_20261006',
    'fixer_forward_media_original_registry_20261006','fixer_forward_media_render_manifest_20261006',
    'fixer_forward_media_history_clearance_20261006',
    'fixer_forward_media_object_read_20261006',
    'fixer_forward_media_lineage_20261006','fixer_forward_media_use_20261006',
    'fixer_forward_media_claim_receipt_20261006'] loop
    execute format('alter table public.%I enable row level security',t);
    execute format('revoke all on public.%I from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006',t);
    -- Selector needs complete fleet occupancy and trusted hashes; backend only.
    if t not in ('fixer_forward_media_original_registry_20261006','fixer_forward_media_render_manifest_20261006',
        'fixer_forward_media_history_clearance_20261006') then
      execute format('grant select on public.%I to service_role',t);
      execute format('create policy service_read on public.%I for select to service_role using(true)',t);
    end if;
  end loop;
end;
$$;

-- RLS is enabled above. Table grants alone cannot admit the dedicated owner:
-- require both role membership and these insert/read policies. No update,
-- delete or truncate privilege is granted; immutable triggers remain in force.
do $$
declare t text;
begin
  foreach t in array array['fixer_forward_media_original_registry_20261006',
    'fixer_forward_media_history_clearance_20261006',
    'fixer_forward_media_render_manifest_20261006'] loop
    execute format('grant select,insert on public.%I to fixer_forward_media_owner_20261006',t);
    execute format('create policy owner_read on public.%I for select to fixer_forward_media_owner_20261006 using(true)',t);
    execute format('create policy owner_insert on public.%I for insert to fixer_forward_media_owner_20261006 with check(true)',t);
  end loop;
end;
$$;

-- Read-only snapshot is accessible by the service and trusted attester. Revision binds the
-- persisted logical content and exact immutable object URLs, excluding mutable
-- status/token so receipts can be generated before the publication claim.
create function public.fixer_forward_media_attestation_request_20261006(p_calendar_row_id uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; tenant text; snapshot jsonb;
begin
  select * into r from public.content_calendar where id=p_calendar_row_id;
  if not found or nullif(btrim(r.gym_id),'') is null
      or r.post_date is null or nullif(btrim(r.visual_group_key),'') is null
      or nullif(btrim(r.source_media_asset_id),'') is null
      or r.render_manifest_digest is null or r.render_manifest_digest !~ '^sha256:[0-9a-f]{64}$'
      or r.source_media_url is null or r.source_media_url !~ '^https://[^[:space:]]+$'
      or r.image_url is null or r.image_url !~ '^https://[^[:space:]]+$'
      or (r.thumbnail_url is not null and r.thumbnail_url !~ '^https://[^[:space:]]+$') then
    raise exception 'persisted source/media identity unavailable' using errcode='23514';
  end if;
  select a.tenant_id into tenant from public.fixer_forward_media_tenant_alias_20261006 a
    where a.alias_key=btrim(r.gym_id);
  tenant:=coalesce(tenant,btrim(r.gym_id));
  snapshot:=jsonb_build_object('calendar_row_id',r.id,'tenant_id',tenant,
    'gym_id',r.gym_id,'account',r.account,'format',r.format,
    'gbp_location_id',r.gbp_location_id,
    'post_date',r.post_date,'group_key',r.visual_group_key,
    'source_asset_id',r.source_media_asset_id,'render_manifest_digest',r.render_manifest_digest,
    'source_url',r.source_media_url,'image_url',r.image_url,'thumbnail_url',r.thumbnail_url);
  return snapshot||jsonb_build_object('revision',md5(snapshot::text));
end;
$$;
revoke all on function public.fixer_forward_media_attestation_request_20261006(uuid)
  from public,anon,authenticated,service_role;
grant execute on function public.fixer_forward_media_attestation_request_20261006(uuid)
  to fixer_forward_media_attester_20261006,service_role;

-- Exact-row lookup exposes only the prepared binding required by the
-- attester. Neither service callers nor the attester can enumerate authority.
create function public.fixer_forward_media_provenance_lookup_20261006(p_calendar_row_id uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare snapshot jsonb; original public.fixer_forward_media_original_registry_20261006%rowtype;
  manifest public.fixer_forward_media_render_manifest_20261006%rowtype;
  clearance public.fixer_forward_media_history_clearance_20261006%rowtype;
begin
  snapshot:=public.fixer_forward_media_attestation_request_20261006(p_calendar_row_id);
  select * into original from public.fixer_forward_media_original_registry_20261006
    where tenant_id=snapshot->>'tenant_id' and source_asset_id=snapshot->>'source_asset_id';
  if not found or original.source_url is distinct from snapshot->>'source_url' then
    raise exception 'authoritative original registry binding unavailable' using errcode='23514';
  end if;
  select * into clearance from public.fixer_forward_media_history_clearance_20261006 c
    where c.tenant_id=original.tenant_id and c.source_asset_id=original.source_asset_id;
  if not found or clearance.decision<>'cleared_unused'
      or clearance.source_url is distinct from original.source_url
      or clearance.source_fingerprint is distinct from original.source_fingerprint
      or clearance.source_length is distinct from original.source_length
      or clearance.registry_evidence_ref is distinct from original.registry_evidence_ref
      or exists(select 1 from public.fixer_forward_media_history_clearance_20261006 c
        where c.source_fingerprint=original.source_fingerprint and c.decision<>'cleared_unused') then
    raise exception 'original historical eligibility clearance unavailable or held' using errcode='23514';
  end if;
  select * into manifest from public.fixer_forward_media_render_manifest_20261006
    where manifest_digest=snapshot->>'render_manifest_digest';
  if not found or manifest.tenant_id is distinct from original.tenant_id
      or manifest.source_asset_id is distinct from original.source_asset_id
      or manifest.image_url is distinct from snapshot->>'image_url'
      or manifest.thumbnail_url is distinct from snapshot->>'thumbnail_url' then
    raise exception 'versioned render manifest binding unavailable' using errcode='23514';
  end if;
  return jsonb_build_object('original',to_jsonb(original),'manifest',to_jsonb(manifest),'clearance',to_jsonb(clearance));
end;
$$;
revoke all on function public.fixer_forward_media_provenance_lookup_20261006(uuid)
  from public,anon,authenticated,service_role;
grant execute on function public.fixer_forward_media_provenance_lookup_20261006(uuid)
  to fixer_forward_media_attester_20261006;

-- Bounded tenant-scoped discovery for the separately credentialed attester.
-- Only unsent active rows with owner-prepared provenance are discoverable.
create function public.fixer_forward_media_pending_attestations_20261006(
  p_tenant_id text,p_limit integer default 50,p_after_row_id uuid default null)
returns setof jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
begin
  if nullif(btrim(p_tenant_id),'') is null or p_limit is null or p_limit<1 or p_limit>100 then
    raise exception 'bounded tenant-scoped attestation request required' using errcode='22023';
  end if;
  return query
    select public.fixer_forward_media_attestation_request_20261006(r.id)
    from public.content_calendar r
    left join public.fixer_forward_media_tenant_alias_20261006 a on a.alias_key=btrim(r.gym_id)
    join public.fixer_forward_media_original_registry_20261006 o
      on o.tenant_id=coalesce(a.tenant_id,btrim(r.gym_id))
        and o.source_asset_id=r.source_media_asset_id and o.source_url=r.source_media_url
    join public.fixer_forward_media_history_clearance_20261006 c
      on c.tenant_id=o.tenant_id and c.source_asset_id=o.source_asset_id
        and c.source_url=o.source_url and c.source_fingerprint=o.source_fingerprint
        and c.source_length=o.source_length and c.registry_evidence_ref=o.registry_evidence_ref
        and c.decision='cleared_unused'
    join public.fixer_forward_media_render_manifest_20261006 m
      on m.manifest_digest=r.render_manifest_digest and m.tenant_id=o.tenant_id
        and m.source_asset_id=o.source_asset_id and m.image_url=r.image_url
        and m.thumbnail_url is not distinct from r.thumbnail_url
    where o.tenant_id=p_tenant_id and r.variant_status='active'
      and (p_after_row_id is null or r.id>p_after_row_id)
      and not exists(select 1 from public.fixer_forward_media_history_clearance_20261006 h
        where h.source_fingerprint=o.source_fingerprint and h.decision<>'cleared_unused')
      and r.post_date is not null and nullif(btrim(r.visual_group_key),'') is not null
      and r.published_at is null and r.late_post_id is null
      and r.status is distinct from 'published' and r.media_not_ready_reason is null
      and not exists(select 1 from public.fixer_forward_media_lineage_20261006 l
        where l.calendar_row_id=r.id and l.row_revision=
          public.fixer_forward_media_attestation_request_20261006(r.id)->>'revision')
    order by r.id limit p_limit;
end;
$$;
revoke all on function public.fixer_forward_media_pending_attestations_20261006(text,integer,uuid)
  from public,anon,authenticated,service_role;
grant execute on function public.fixer_forward_media_pending_attestations_20261006(text,integer,uuid)
  to fixer_forward_media_attester_20261006;

create function public.fixer_attest_forward_media_20261006(
  p_calendar_row_id uuid,p_expected_revision text,p_evidence_id uuid,
  p_source_fingerprint text,p_source_length bigint,p_image_fingerprint text,p_image_length bigint,
  p_thumbnail_fingerprint text,p_thumbnail_length bigint,p_operation text,p_evidence_ref text
) returns uuid language plpgsql security definer set search_path=pg_catalog,public
set lock_timeout='5s' as $$
declare
  snapshot jsonb; provenance jsonb; tenant text; urls text[]; hashes text[]; lengths bigint[];
  ids uuid[]:=array[]::uuid[]; rid uuid; i integer; old public.fixer_forward_media_object_read_20261006%rowtype;
begin
  -- Exclusive graph authority: no claim may observe an incomplete ancestry
  -- graph while a trusted attester is appending edges. Acquire BEFORE any row
  -- lock so a claim holding the shared graph lock cannot deadlock on this row.
  -- Read Committed refreshes statement snapshots after advisory-lock waits;
  -- repeatable snapshots could otherwise miss the edge that just committed.
  if current_setting('transaction_isolation')<>'read committed' then
    raise exception 'forward media authority requires read committed isolation' using errcode='25000';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
  -- Take census authority before calendar rows: provenance below re-enters
  -- this lock, preserving the same order as census-protected writers.
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
  -- Serialize with a publisher or concurrent edit before deriving the snapshot.
  perform 1 from public.content_calendar where id=p_calendar_row_id for update;
  snapshot:=public.fixer_forward_media_attestation_request_20261006(p_calendar_row_id);
  if p_expected_revision is null or snapshot->>'revision' is distinct from p_expected_revision
      or p_evidence_id is null or nullif(btrim(p_evidence_ref),'') is null
      or p_operation is null or p_operation not in ('same_object','render','reburn','rehost') then
    raise exception 'attestation revision or controlled operation invalid' using errcode='23514';
  end if;
  provenance:=public.fixer_forward_media_provenance_lookup_20261006(p_calendar_row_id);
  if provenance#>>'{original,source_fingerprint}' is distinct from p_source_fingerprint
      or (provenance#>>'{original,source_length}')::bigint is distinct from p_source_length
      or provenance#>>'{manifest,image_fingerprint}' is distinct from p_image_fingerprint
      or (provenance#>>'{manifest,image_length}')::bigint is distinct from p_image_length
      or provenance#>>'{manifest,thumbnail_fingerprint}' is distinct from p_thumbnail_fingerprint
      or (provenance#>>'{manifest,thumbnail_length}')::bigint is distinct from p_thumbnail_length
      or provenance#>>'{manifest,operation}' is distinct from p_operation then
    raise exception 'fetched bytes or operation differ from authoritative provenance' using errcode='23514';
  end if;
  tenant:=snapshot->>'tenant_id';
  urls:=array[snapshot->>'source_url',snapshot->>'image_url',snapshot->>'thumbnail_url'];
  hashes:=array[p_source_fingerprint,p_image_fingerprint,p_thumbnail_fingerprint];
  lengths:=array[p_source_length,p_image_length,p_thumbnail_length];
  if (urls[3] is null) is distinct from (hashes[3] is null and lengths[3] is null) then
    raise exception 'thumbnail evidence differs from persisted object' using errcode='23514';
  end if;
  if p_operation='same_object' and (urls[1] is distinct from urls[2]
      or hashes[1] is distinct from hashes[2] or lengths[1] is distinct from lengths[2]
      or (urls[3] is not null and (urls[3] is distinct from urls[1]
        or hashes[3] is distinct from hashes[1] or lengths[3] is distinct from lengths[1]))) then
    raise exception 'same-object attestation cannot invent ancestry' using errcode='23514';
  end if;
  for i in 1..3 loop
    if urls[i] is null then ids:=array_append(ids,null::uuid); continue; end if;
    if hashes[i] is null or hashes[i] !~ '^md5:[0-9a-f]{32}$'
        or lengths[i] is null or lengths[i]<=0 or lengths[i]>134217728 then
      raise exception 'verified bounded byte evidence required' using errcode='23514';
    end if;
    -- Sort URL locks to avoid opposing source/rendition order deadlocks.
  end loop;
  perform pg_advisory_xact_lock(hashtextextended(jsonb_build_array('fixer_read',tenant,u)::text,0))
    from (select distinct u from unnest(urls) u where u is not null order by u) objects;
  ids:=array[]::uuid[];
  for i in 1..3 loop
    if urls[i] is null then ids:=array_append(ids,null::uuid); continue; end if;
    rid:=gen_random_uuid();
    insert into public.fixer_forward_media_object_read_20261006
      (receipt_id,tenant_id,exact_url,fingerprint,byte_length,evidence_ref,verified_by)
      values(rid,tenant,urls[i],hashes[i],lengths[i],p_evidence_ref,'trusted_attester')
      on conflict(tenant_id,exact_url) do nothing;
    select * into old from public.fixer_forward_media_object_read_20261006
      where tenant_id=tenant and exact_url=urls[i];
    if old.fingerprint is distinct from hashes[i] or old.byte_length is distinct from lengths[i] then
      raise exception 'immutable object URL changed bytes; fresh versioned URL required' using errcode='23514';
    end if;
    ids:=array_append(ids,old.receipt_id);
  end loop;
  insert into public.fixer_forward_media_lineage_20261006
    (evidence_id,calendar_row_id,row_revision,operation,tenant_id,group_key,
     source_read_receipt,image_read_receipt,thumbnail_read_receipt,source_asset_id,manifest_digest,render_evidence_ref,verified_by)
    values(p_evidence_id,p_calendar_row_id,p_expected_revision,p_operation,tenant,snapshot->>'group_key',
      ids[1],ids[2],ids[3],snapshot->>'source_asset_id',snapshot->>'render_manifest_digest',p_evidence_ref,'trusted_attester');
  return p_evidence_id;
end;
$$;
revoke all on function public.fixer_attest_forward_media_20261006(uuid,text,uuid,text,bigint,text,bigint,text,bigint,text,text)
  from public,anon,authenticated,service_role;
grant execute on function public.fixer_attest_forward_media_20261006(uuid,text,uuid,text,bigint,text,bigint,text,bigint,text,text)
  to fixer_forward_media_attester_20261006;

create function public.fixer_claim_forward_media_20261006(
  p_calendar_row_id uuid,p_claim_token uuid,p_evidence_id uuid,p_expected_revision text
) returns boolean language plpgsql security definer
set search_path=pg_catalog,public set lock_timeout='5s' as $$
declare
  r public.content_calendar%rowtype;
  tenant text;
  hashes text[];
  fp text;
  evidence uuid;
  receipt public.fixer_forward_media_claim_receipt_20261006%rowtype;
  occupied public.fixer_forward_media_use_20261006%rowtype;
  replay boolean:=false;
begin
  if p_calendar_row_id is null or p_claim_token is null or p_evidence_id is null
      or nullif(btrim(p_expected_revision),'') is null then
    raise exception 'existing owned calendar claim required' using errcode='22023';
  end if;
  -- Shared graph authority persists through occupancy/receipt commit. The
  -- separate census lock below serializes final claims and census mutations;
  -- attestation graph mutation waits without a publisher graph upgrade.
  -- Take this lock before row locks, matching the attester lock order.
  if current_setting('transaction_isolation')<>'read committed' then
    raise exception 'forward media authority requires read committed isolation' using errcode='25000';
  end if;
  perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
  -- Separate exclusive census authority, acquired before any row lock. No
  -- publisher upgrades the shared ancestry graph; calendar census writes use
  -- this same transaction lock and cannot commit between proof and receipt.
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
  select * into r from public.content_calendar where id=p_calendar_row_id for update;
  if not found or r.publish_claim_token is distinct from p_claim_token
      or r.status is null or r.status not in ('publishing','published')
      or r.variant_status is distinct from 'active'
      or r.post_date is null
      or r.media_not_ready_reason is not null
      or nullif(btrim(r.visual_group_key),'') is null then
    raise exception 'calendar claim ownership or ready media invalid' using errcode='23514';
  end if;
  -- Bind exactly the caller's outgoing media snapshot under the row lock.
  -- A concurrent edit/re-attestation cannot authorize different outbound bytes.
  if public.fixer_forward_media_attestation_request_20261006(r.id)->>'revision'
      is distinct from p_expected_revision then
    raise exception 'outgoing media revision changed' using errcode='23514';
  end if;
  -- The publisher must bind its gym-local attempt day under the owned token
  -- before this RPC. Never infer it from UTC or the content date: catch-up may
  -- send on a different local day.
  if r.publish_reservation_day is null then
    raise exception 'owned publishing reservation day unavailable' using errcode='23514';
  end if;
  select a.tenant_id into tenant from public.fixer_forward_media_tenant_alias_20261006 a
    where a.alias_key=btrim(r.gym_id);
  tenant:=coalesce(tenant,nullif(btrim(r.gym_id),''));
  if tenant is null then raise exception 'canonical tenant mapping unavailable' using errcode='23514'; end if;
  if not exists(select 1 from public.fixer_forward_media_claim_gate_20261006 g
      where g.tenant_id=tenant and g.enabled) then
    raise exception 'forward media guard is OFF pending history and publisher review' using errcode='55000';
  end if;
  -- Recheck historical clearance at each send boundary, including receipt
  -- replay. A trusted lineage record never substitutes for historical eligibility.
  perform public.fixer_forward_media_provenance_lookup_20261006(r.id);
  -- No row-provided byte_hash, asset ID or URL-derived identity is trusted.
  select l.evidence_id, array(select distinct h from unnest(array[s.fingerprint,i.fingerprint,t.fingerprint]) h
      where h is not null order by h)
    into evidence,hashes
    from public.fixer_forward_media_lineage_20261006 l
    join public.fixer_forward_media_object_read_20261006 s on s.receipt_id=l.source_read_receipt
    join public.fixer_forward_media_object_read_20261006 i on i.receipt_id=l.image_read_receipt
    left join public.fixer_forward_media_object_read_20261006 t on t.receipt_id=l.thumbnail_read_receipt
    where l.evidence_id=p_evidence_id and l.calendar_row_id=r.id
      and l.row_revision=public.fixer_forward_media_attestation_request_20261006(r.id)->>'revision'
      and l.tenant_id=tenant and l.group_key=r.visual_group_key
      and l.source_asset_id=r.source_media_asset_id and l.manifest_digest=r.render_manifest_digest
      and s.tenant_id=tenant and i.tenant_id=tenant
      and s.exact_url=r.source_media_url and i.exact_url=r.image_url
      and ((r.thumbnail_url is null and l.thumbnail_read_receipt is null)
        or (r.thumbnail_url is not null and t.tenant_id=tenant and t.exact_url=r.thumbnail_url))
      and ((s.receipt_id=i.receipt_id and (t.receipt_id is null or t.receipt_id=s.receipt_id))
        or nullif(btrim(l.render_evidence_ref),'') is not null)
    order by l.evidence_id limit 1;
  if evidence is null or hashes is null or cardinality(hashes)=0 then
    raise exception 'original source and delivered bytes must be owner attested' using errcode='23514';
  end if;
  -- Freeze replay identity before expanding the current fleet graph. The
  -- immutable receipt proves the exact earlier reservation; benign later
  -- lineage must not change the byte set that this token already committed.
  perform pg_advisory_xact_lock(hashtextextended(
    jsonb_build_array('fixer_forward_token_20261006',p_claim_token)::text,0));
  select * into receipt from public.fixer_forward_media_claim_receipt_20261006 where claim_token=p_claim_token;
  replay:=found;
  if replay then
    if receipt.calendar_row_id is distinct from r.id
      or receipt.tenant_id is distinct from tenant or receipt.post_date is distinct from r.post_date
      or receipt.reservation_day is distinct from r.publish_reservation_day
      or receipt.group_key is distinct from r.visual_group_key
      or receipt.evidence_id is distinct from evidence
      or not (hashes <@ receipt.fingerprints)
      or receipt.source_url is distinct from r.source_media_url
      or receipt.image_url is distinct from r.image_url or receipt.thumbnail_url is distinct from r.thumbnail_url then
      raise exception 'claim token receipt differs from persisted media' using errcode='23514';
    end if;
  end if;
  -- A previously attested derivative cannot become a fresh original by
  -- rehosting/relabeling its bytes. Follow trusted source edges fleet-wide,
  -- including thumbnail ancestry, until the complete known closure is reached.
  -- UNION deduplicates fingerprints and safely terminates cyclic/self edges.
  with recursive ancestry(fingerprint) as (
    select h from unnest(hashes) h
    union
    select source.fingerprint
      from ancestry a
      join public.fixer_forward_media_object_read_20261006 delivered
        on delivered.fingerprint=a.fingerprint
      join public.fixer_forward_media_lineage_20261006 edge
        on edge.image_read_receipt=delivered.receipt_id
          or edge.thumbnail_read_receipt=delivered.receipt_id
      join public.fixer_forward_media_object_read_20261006 source
        on source.receipt_id=edge.source_read_receipt
  ) select array_agg(fingerprint order by fingerprint) into hashes from ancestry;
  if exists(select 1 from public.fixer_forward_media_history_clearance_20261006 c
      where c.source_fingerprint=any(hashes) and c.decision<>'cleared_unused') then
    raise exception 'original or rendition historical eligibility held' using errcode='23514';
  end if;
  -- Deterministically sorted GLOBAL byte locks. Check current ancestry for
  -- new history holds and conflicting use, but keep replay occupancy proof
  -- bound to the frozen receipt rather than requiring newly known bytes to
  -- have been reserved by a transaction that preceded their discovery.
  foreach fp in array hashes loop
    perform pg_advisory_xact_lock(hashtextextended(
      jsonb_build_array('fixer_forward_byte_20261006',fp)::text,0));
  end loop;
  if replay then
    foreach fp in array receipt.fingerprints loop
      select * into occupied from public.fixer_forward_media_use_20261006 where fingerprint=fp;
      if not found or occupied.tenant_id is distinct from tenant
          or occupied.post_date is distinct from r.post_date or occupied.group_key is distinct from r.visual_group_key then
        raise exception 'claim receipt occupancy unavailable or inconsistent' using errcode='23514';
      end if;
    end loop;
    -- A later edge with no conflicting use is harmless. Newly discovered
    -- consumed ancestry still holds the send boundary, including exact retries.
    foreach fp in array hashes loop
      select * into occupied from public.fixer_forward_media_use_20261006 where fingerprint=fp;
      if found and (occupied.tenant_id is distinct from tenant
          or occupied.post_date is distinct from r.post_date or occupied.group_key is distinct from r.visual_group_key) then
        raise exception 'source or rendition already consumed by another tenant/date/group' using errcode='23514';
      end if;
    end loop;
    return true;
  end if;
  if r.status<>'publishing' or r.published_at is not null or r.late_post_id is not null then
    raise exception 'new forward claim requires an unsent publishing row' using errcode='23514';
  end if;
  foreach fp in array hashes loop
    insert into public.fixer_forward_media_use_20261006
      (fingerprint,tenant_id,post_date,group_key,first_calendar_row_id,first_claim_token)
      values(fp,tenant,r.post_date,r.visual_group_key,r.id,p_claim_token)
      on conflict(fingerprint) do nothing;
    select * into occupied from public.fixer_forward_media_use_20261006 where fingerprint=fp;
    if not found or occupied.tenant_id is distinct from tenant
        or occupied.post_date is distinct from r.post_date or occupied.group_key is distinct from r.visual_group_key then
      raise exception 'source or rendition already consumed by another tenant/date/group' using errcode='23514';
    end if;
  end loop;
  insert into public.fixer_forward_media_claim_receipt_20261006
    (claim_token,calendar_row_id,tenant_id,post_date,reservation_day,group_key,evidence_id,
     fingerprints,source_url,image_url,thumbnail_url)
    values(p_claim_token,r.id,tenant,r.post_date,r.publish_reservation_day,r.visual_group_key,evidence,
      hashes,r.source_media_url,r.image_url,r.thumbnail_url);
  return true;
end;
$$;
revoke all on function public.fixer_claim_forward_media_20261006(uuid,uuid,uuid,text) from public,anon,authenticated,service_role;
grant execute on function public.fixer_claim_forward_media_20261006(uuid,uuid,uuid,text) to service_role;

-- Service-role binding RPC: the ONLY authority that may attach a persisted
-- render manifest digest to a calendar row. It accepts only the row ID; a
-- caller can never supply a digest, hash, URL or history claim. The owner
-- registry/clearance/manifest evidence must already exist; this function
-- never writes owner authority, never sends, and never touches approval or
-- status. Fail closed on missing or ambiguous persisted binding.
create function public.fixer_bind_forward_media_manifest_20261006(p_calendar_row_id uuid)
returns boolean language plpgsql security definer set search_path=pg_catalog,public
set lock_timeout='5s' as $$
declare
  r public.content_calendar%rowtype; tenant text; digest text; matches integer;
  original public.fixer_forward_media_original_registry_20261006%rowtype;
begin
  if current_setting('transaction_isolation')<>'read committed' then
    raise exception 'forward media authority requires read committed isolation' using errcode='25000';
  end if;
  -- Match the graph lock order used by attestation and claims. Owner clearance
  -- and manifest inserts take the exclusive lock before this shared lookup.
  perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
  -- Serialize with any publisher/attester on this exact row before binding.
  select * into r from public.content_calendar where id=p_calendar_row_id for update;
  -- Require an unsent active row with a complete explicit persisted identity:
  -- source asset, source URL, image URL, optional thumbnail, group and tenant.
  if not found or r.status not in ('draft','pending','queued','approved')
      or r.publish_claim_token is not null
      or r.published_at is not null or r.late_post_id is not null
      or r.variant_status is distinct from 'active'
      or r.post_date is null
      or nullif(btrim(r.gym_id),'') is null or nullif(btrim(r.visual_group_key),'') is null
      or nullif(btrim(r.source_media_asset_id),'') is null
      or r.source_media_url is null or r.source_media_url !~ '^https://[^[:space:]]+$'
      or r.image_url is null or r.image_url !~ '^https://[^[:space:]]+$'
      or (r.thumbnail_url is not null and r.thumbnail_url !~ '^https://[^[:space:]]+$') then
    raise exception 'binding requires an unsent active row with complete explicit source and media identity' using errcode='23514';
  end if;
  select a.tenant_id into tenant from public.fixer_forward_media_tenant_alias_20261006 a
    where a.alias_key=btrim(r.gym_id);
  tenant:=coalesce(tenant,btrim(r.gym_id));
  -- The authoritative original must match the row's explicit tenant/asset/URL.
  select * into original from public.fixer_forward_media_original_registry_20261006 o
    where o.tenant_id=tenant and o.source_asset_id=btrim(r.source_media_asset_id);
  if not found or original.source_url is distinct from r.source_media_url then
    raise exception 'authoritative original registry binding unavailable' using errcode='23514';
  end if;
  -- Cleared history for the exact registry tuple and no fleet hold anywhere
  -- for these source bytes; missing or held history refuses the bind.
  if not exists(select 1 from public.fixer_forward_media_history_clearance_20261006 c
      where c.tenant_id=original.tenant_id and c.source_asset_id=original.source_asset_id
        and c.source_url=original.source_url and c.source_fingerprint=original.source_fingerprint
        and c.source_length=original.source_length and c.registry_evidence_ref=original.registry_evidence_ref
        and c.decision='cleared_unused')
      or exists(select 1 from public.fixer_forward_media_history_clearance_20261006 c
        where c.source_fingerprint=original.source_fingerprint and c.decision<>'cleared_unused') then
    raise exception 'original historical eligibility clearance unavailable or held' using errcode='23514';
  end if;
  -- Exactly ONE immutable manifest must match the persisted tenant, source
  -- asset, image URL and thumbnail URL. Zero or many fail closed.
  select count(*), min(m.manifest_digest) into matches, digest
    from public.fixer_forward_media_render_manifest_20261006 m
    where m.tenant_id=tenant and m.source_asset_id=original.source_asset_id
      and m.image_url=r.image_url and m.thumbnail_url is not distinct from r.thumbnail_url;
  if matches is distinct from 1 then
    raise exception 'exactly one matching immutable render manifest is required' using errcode='23514';
  end if;
  if r.render_manifest_digest is null then
    update public.content_calendar set render_manifest_digest=digest where id=r.id;
    return true;
  end if;
  if r.render_manifest_digest=digest then
    return true;
  end if;
  raise exception 'row is already bound to a conflicting render manifest digest' using errcode='23514';
end;
$$;
revoke all on function public.fixer_bind_forward_media_manifest_20261006(uuid)
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006;
grant execute on function public.fixer_bind_forward_media_manifest_20261006(uuid) to service_role;

-- Existing table-level INSERT/UPDATE grants must not bypass the validated binder.
-- SECURITY INVOKER retains the binder's effective owner while its definer
-- body updates the row, but sees service_role on a direct service UPDATE.
create function public.fixer_guard_forward_media_digest_20261006()
returns trigger language plpgsql security invoker set search_path=pg_catalog,public as $$
begin
  if ((tg_op='INSERT' and new.render_manifest_digest is not null)
      or (tg_op='UPDATE' and new.render_manifest_digest is distinct from old.render_manifest_digest))
      and current_user::regrole::oid is distinct from (
        select p.proowner from pg_catalog.pg_proc p
        where p.oid='public.fixer_bind_forward_media_manifest_20261006(uuid)'::regprocedure
      ) then
    raise exception 'render manifest digest changes require the validated binder' using errcode='42501';
  end if;
  return new;
end;
$$;
revoke all on function public.fixer_guard_forward_media_digest_20261006()
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006;
create trigger fixer_forward_media_digest_guard_20261006
  before insert or update of render_manifest_digest on public.content_calendar
  for each row execute function public.fixer_guard_forward_media_digest_20261006();
commit;
