-- DRAFT / UNAPPLIED / DEFAULT OFF. No production activation is authorized.
-- Requires DRAFT_fixer_forward_schedule_reservation_20261008.sql (reservation
-- stack, gate and legacy atomic finalizer) and
-- DRAFT_fixer_forward_media_observation_bridge_20261007.sql (unverified
-- observation table). This draft adds the TWO-PHASE staging authority:
--
--   1. stage_forward_schedule_batch_20261008 atomically persists an immutable
--      batch identity (caller-stable batch UUID + exact sha256 request
--      digest), complete candidate membership with content/media binding,
--      the PERSISTED exact old pending/draft row IDs with their full CAS
--      snapshots (every column including NULLs, validated against the live
--      rows at stage time), the inactive candidate calendar rows
--      (variant_status='candidate',
--      media_not_ready_reason='forward_reservation_staged', status
--      pending/draft, never approved, never claimed/sent) and their
--      UNVERIFIED observations in ONE transaction. The old-row snapshots are
--      part of the exact request bytes, so the immutable digest binds them.
--      An exact retry (same
--      batch UUID, same digest) returns the same receipt without rewriting;
--      the same batch UUID with a changed digest is refused. No candidate is
--      activated, reserved, attested or approved here; no old row is archived.
--   2. forward_schedule_preparation_eligible_20261008 is the ONLY narrow
--      predicate that may authorize isolated owner/photo/binder/attester
--      preparation for a staged row: exact registered nonterminal batch
--      membership, canonical tenant match, immutable content/media binding
--      against the member record, a FULL-ROW comparison of the live row
--      against the persisted staged_snapshot (only the trusted preparation
--      output fields source_media_asset_id and render_manifest_digest may
--      differ; every other column -- account, format, caption,
--      visual_group_key, dates, URLs, tenant keys, claim/send state -- must
--      equal the staged bytes exactly), candidate state with the exact stage
--      marker, and no claim/send/approval. It authorizes PREPARATION ONLY;
--      final publish/claim remains active-only through the existing claim
--      stack, which is unchanged here.
--   3. finalize_forward_schedule_staged_batch_20261008 re-checks the complete
--      batch membership against the persisted immutable record, re-validates
--      every candidate row against its persisted staged_snapshot with the
--      same full-row comparison (only the trusted preparation output fields
--      source_media_asset_id and render_manifest_digest may differ; any
--      caller edit to planned content/media/tenant fields holds the batch),
--      and uses ONLY the PERSISTED old-row set: a caller-supplied old-row
--      set that differs
--      from the persisted snapshots in any way is refused, every persisted
--      old row is CAS re-validated against its live row (a row changed after
--      staging holds the batch), and only the persisted old IDs are archived
--      (a row added after staging is never archived). It then runs the same
--      sorted-row locking, old-row CAS, activation, reservation and archive
--      steps as the legacy finalizer, and writes the terminal receipt into
--      the batch row in the SAME transaction. An exact finalize retry returns
--      the persisted receipt; a changed finalize request against a finalized
--      batch is refused. An ambiguous network outcome is resolved ONLY by
--      forward_schedule_batch_status_20261008 readback (batch ID, tenant,
--      digest, exact member + old-row sets and complete terminal receipt),
--      never by inferring success from mutable calendar rows.
--
-- Lock order is unchanged: shared graph -> exclusive census -> batch row ->
-- sorted calendar rows -> slot/byte/token locks inside the existing stack.
-- Production variant states remain active/candidate/archived; this draft
-- introduces no new variant_status or calendar status value. No trusted owner
-- or attester credential enters this file; the predicate is granted to those
-- isolated roles so their own workers can verify eligibility themselves.
--
-- Deliberately OUT OF SCOPE (documented, not broadened unsafely): the
-- existing discovery/prefilter functions
-- fixer_record_forward_media_observation_20261007,
-- fixer_forward_media_owner_pending_20261007,
-- fixer_owner_photo_pending_20261007 / fixer_prepare_owner_photo_20261007 and
-- fixer_forward_media_pending_attestations_20261006 still require active rows
-- and do NOT consult this predicate. Wiring them to staged membership needs a
-- separately reviewed migration; this draft only provides the predicate and
-- the durable records they must join. Attester discovery also still drops a
-- row once lineage exists (missing-visual-role recovery remains a separate
-- release blocker documented in docs/FORWARD_SCHEDULE_RESERVATION_20261008.md).
-- Rollback before use: remove the new objects. After use preserve all rows.
begin;

-- Hard prerequisites: reservation stack + observation bridge. Fail whole.
do $$
begin
  if not exists(select 1 from pg_catalog.pg_proc p
    join pg_catalog.pg_namespace n on n.oid=p.pronamespace
    where n.nspname='public' and p.proname='reserve_forward_slot_20261008') then
    raise exception 'forward schedule reservation draft is required' using errcode='23514';
  end if;
  if to_regclass('public.fixer_forward_media_observation_20261007') is null then
    raise exception 'forward media observation bridge draft is required' using errcode='23514';
  end if;
end;
$$;

-- Immutable batch identity + exact request digest + terminal receipt. The
-- receipt is the ONLY resolution of an ambiguous finalize response.
create table public.forward_schedule_stage_batch_20261008 (
  batch_id uuid primary key,
  tenant_id text not null check (tenant_id=btrim(tenant_id) and tenant_id<>''),
  request_digest text not null check (request_digest ~ '^[0-9a-f]{64}$'),
  request_payload jsonb not null,
  state text not null default 'staged' check (state in ('staged','finalized')),
  finalize_request jsonb,
  finalize_receipt jsonb,
  created_at timestamptz not null default now(),
  finalized_at timestamptz,
  check ((state='finalized')=(finalize_request is not null
         and finalize_receipt is not null and finalized_at is not null))
);
alter table public.forward_schedule_stage_batch_20261008 enable row level security;
revoke all on public.forward_schedule_stage_batch_20261008
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant select on public.forward_schedule_stage_batch_20261008 to service_role;
create policy service_read on public.forward_schedule_stage_batch_20261008
  for select to service_role using(true);

-- Complete candidate membership with immutable content/media binding. A
-- member row can never move batches, change binding or be deleted.
create table public.forward_schedule_stage_member_20261008 (
  batch_id uuid not null references public.forward_schedule_stage_batch_20261008(batch_id),
  position integer not null check (position>=0),
  calendar_row_id uuid not null unique,
  logical_post_id uuid not null,
  post_date date not null,
  gym_id text not null check (gym_id=btrim(gym_id) and gym_id<>''),
  tenant_id text not null check (tenant_id=btrim(tenant_id) and tenant_id<>''),
  source_media_url text not null check (source_media_url ~ '^https://[^[:space:]]+$'),
  image_url text not null check (image_url ~ '^https://[^[:space:]]+$'),
  thumbnail_url text check (thumbnail_url is null or thumbnail_url ~ '^https://[^[:space:]]+$'),
  observation_digest text check (observation_digest is null or observation_digest ~ '^[0-9a-f]{64}$'),
  staged_snapshot jsonb not null,
  primary key (batch_id,position)
);
alter table public.forward_schedule_stage_member_20261008 enable row level security;
revoke all on public.forward_schedule_stage_member_20261008
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant select on public.forward_schedule_stage_member_20261008 to service_role;
create policy service_read on public.forward_schedule_stage_member_20261008
  for select to service_role using(true);

-- The PERSISTED exact old-row set: full CAS snapshots (every column incl.
-- NULLs) of the old pending/draft rows the batch replaces, validated against
-- the live rows at stage time and bound to the batch + immutable request
-- digest. Finalization uses ONLY this set; it never trusts a caller set.
create table public.forward_schedule_stage_old_row_20261008 (
  batch_id uuid not null references public.forward_schedule_stage_batch_20261008(batch_id),
  position integer not null check (position>=0),
  calendar_row_id uuid not null unique,
  tenant_id text not null check (tenant_id=btrim(tenant_id) and tenant_id<>''),
  logical_post_id uuid,
  post_date date,
  old_snapshot jsonb not null,
  primary key (batch_id,position)
);
alter table public.forward_schedule_stage_old_row_20261008 enable row level security;
revoke all on public.forward_schedule_stage_old_row_20261008
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant select on public.forward_schedule_stage_old_row_20261008 to service_role;
create policy service_read on public.forward_schedule_stage_old_row_20261008
  for select to service_role using(true);

-- Write guards: only the stage/finalize definer RPCs (through their owner)
-- may write. Batch identity/payload are immutable; state is one-way
-- staged -> finalized. Membership is fully immutable durable evidence.
create function public.fixer_forward_stage_batch_guard_20261008()
returns trigger language plpgsql security invoker set search_path=pg_catalog,public as $$
begin
  if current_user::regrole::oid is distinct from (
    select p.proowner from pg_catalog.pg_proc p
    where p.oid='public.stage_forward_schedule_batch_20261008(text,uuid,text,text)'::regprocedure
  ) then
    raise exception 'forward schedule stage batches are RPC-managed only' using errcode='42501';
  end if;
  if tg_op='DELETE' then
    raise exception 'forward schedule stage batches are durable; deletion forbidden' using errcode='23514';
  end if;
  if tg_op='INSERT' and (new.state is distinct from 'staged'
      or new.finalize_request is not null or new.finalize_receipt is not null
      or new.finalized_at is not null) then
    raise exception 'forward schedule stage batches are inserted staged only' using errcode='23514';
  end if;
  if tg_op='UPDATE' then
    if old.state is distinct from 'staged' or new.state is distinct from 'finalized'
        or new.batch_id is distinct from old.batch_id
        or new.tenant_id is distinct from old.tenant_id
        or new.request_digest is distinct from old.request_digest
        or new.request_payload is distinct from old.request_payload
        or new.created_at is distinct from old.created_at then
      raise exception 'forward schedule stage batch identity is immutable; only one-way finalization allowed' using errcode='23514';
    end if;
  end if;
  return coalesce(new,old);
end;
$$;
revoke all on function public.fixer_forward_stage_batch_guard_20261008()
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
create trigger stage_batch_guard before insert or update or delete on public.forward_schedule_stage_batch_20261008
  for each row execute function public.fixer_forward_stage_batch_guard_20261008();
create trigger stage_batch_guard_truncate before truncate on public.forward_schedule_stage_batch_20261008
  for each statement execute function public.fixer_forward_media_immutable_20261006();

create function public.fixer_forward_stage_member_guard_20261008()
returns trigger language plpgsql security invoker set search_path=pg_catalog,public as $$
begin
  if current_user::regrole::oid is distinct from (
    select p.proowner from pg_catalog.pg_proc p
    where p.oid='public.stage_forward_schedule_batch_20261008(text,uuid,text,text)'::regprocedure
  ) then
    raise exception 'forward schedule stage membership is RPC-managed only' using errcode='42501';
  end if;
  if tg_op<>'INSERT' then
    raise exception 'forward schedule stage membership is immutable durable evidence' using errcode='23514';
  end if;
  return new;
end;
$$;
revoke all on function public.fixer_forward_stage_member_guard_20261008()
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
create trigger stage_member_guard before insert or update or delete on public.forward_schedule_stage_member_20261008
  for each row execute function public.fixer_forward_stage_member_guard_20261008();
create trigger stage_member_guard_truncate before truncate on public.forward_schedule_stage_member_20261008
  for each statement execute function public.fixer_forward_media_immutable_20261006();

create function public.fixer_forward_stage_old_row_guard_20261008()
returns trigger language plpgsql security invoker set search_path=pg_catalog,public as $$
begin
  if current_user::regrole::oid is distinct from (
    select p.proowner from pg_catalog.pg_proc p
    where p.oid='public.stage_forward_schedule_batch_20261008(text,uuid,text,text)'::regprocedure
  ) then
    raise exception 'forward schedule stage old rows are RPC-managed only' using errcode='42501';
  end if;
  if tg_op<>'INSERT' then
    raise exception 'forward schedule stage old rows are immutable durable evidence' using errcode='23514';
  end if;
  return new;
end;
$$;
revoke all on function public.fixer_forward_stage_old_row_guard_20261008()
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
create trigger stage_old_row_guard before insert or update or delete on public.forward_schedule_stage_old_row_20261008
  for each row execute function public.fixer_forward_stage_old_row_guard_20261008();
create trigger stage_old_row_guard_truncate before truncate on public.forward_schedule_stage_old_row_20261008
  for each statement execute function public.fixer_forward_media_immutable_20261006();

-- Atomic two-phase STAGE. p_request is the exact raw JSON text
--   {"members":[{"row":{<content_calendar columns>},
--                "observation":{"observation_json":<text>,"digest_input":<text>}|null}],
--    "old_rows":[{<every content_calendar column of an old row, incl. NULLs>}]}
-- and p_request_digest is the lowercase sha256 hex of that exact string, so
-- the digest is independently recomputed here; a planner cannot claim a
-- digest for bytes it did not send. The old-row snapshots are part of those
-- exact bytes: each must equal the CURRENT live row (full-column CAS), be
-- active pending/draft, unprotected and same-tenant, and is persisted as
-- durable evidence. Creates inactive candidate rows and unverified
-- observations atomically; ANY failure rolls back everything. No old row is
-- archived here.
create function public.stage_forward_schedule_batch_20261008(
  p_tenant_id text,p_batch_id uuid,p_request text,p_request_digest text
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare
  payload jsonb; members jsonb; member jsonb; obs jsonb; observation jsonb;
  old_item jsonb; old_snapshot jsonb;
  b public.forward_schedule_stage_batch_20261008%rowtype;
  r public.content_calendar%rowtype; inserted public.content_calendar%rowtype;
  existing public.fixer_forward_media_observation_20261007%rowtype;
  tenant text; revision text; digest text; snapshot jsonb;
  member_ids uuid[]:=array[]::uuid[]; observed_ids uuid[]:=array[]::uuid[];
  old_ids uuid[]:=array[]::uuid[];
  pos integer:=-1; old_pos integer:=-1; inserted_batches integer;
begin
  if nullif(btrim(p_tenant_id),'') is null or p_batch_id is null or p_request is null
      or octet_length(p_request)<2 or octet_length(p_request)>1048576 then
    raise exception 'exact stage batch binding required' using errcode='22023';
  end if;
  if p_request_digest is null or p_request_digest !~ '^[0-9a-f]{64}$'
      or p_request_digest is distinct from encode(sha256(convert_to(p_request,'UTF8')),'hex') then
    raise exception 'stage batch request digest invalid' using errcode='22023';
  end if;
  payload:=p_request::jsonb;
  if jsonb_typeof(payload) is distinct from 'object'
      or jsonb_typeof(payload->'members') is distinct from 'array'
      or jsonb_array_length(payload->'members')=0
      or jsonb_array_length(payload->'members')>100
      or jsonb_typeof(payload->'old_rows') is distinct from 'array'
      or jsonb_array_length(payload->'old_rows')>100 then
    raise exception 'exact stage batch membership required' using errcode='22023';
  end if;
  if current_setting('transaction_isolation')<>'read committed' then
    raise exception 'forward media authority requires read committed isolation' using errcode='25000';
  end if;
  -- Lock order: shared graph -> exclusive census -> batch row -> calendar rows.
  perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
  if not exists(select 1 from public.forward_schedule_reservation_gate_20261008 where singleton and enabled) then
    raise exception 'schedule reservation authority is OFF pending review' using errcode='55000';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
  -- Idempotency: first writer wins the batch identity. An exact retry reads
  -- back the same receipt; a changed digest for the same batch UUID refuses.
  insert into public.forward_schedule_stage_batch_20261008
    (batch_id,tenant_id,request_digest,request_payload)
    values(p_batch_id,btrim(p_tenant_id),p_request_digest,payload)
    on conflict (batch_id) do nothing;
  get diagnostics inserted_batches=row_count;
  select * into b from public.forward_schedule_stage_batch_20261008
    where batch_id=p_batch_id for update;
  if b.request_digest is distinct from p_request_digest or b.tenant_id is distinct from btrim(p_tenant_id) then
    raise exception 'stage batch request changed' using errcode='23514';
  end if;
  if inserted_batches=0 then
    -- Exact retry: return the persisted receipt without rewriting anything.
    return jsonb_build_object('batch_id',b.batch_id,'tenant_id',b.tenant_id,
      'request_digest',b.request_digest,'state',b.state,
      'member_row_ids',(select coalesce(jsonb_agg(m.calendar_row_id order by m.position),'[]'::jsonb)
        from public.forward_schedule_stage_member_20261008 m where m.batch_id=b.batch_id),
      'observation_row_ids',(select coalesce(jsonb_agg(m.calendar_row_id order by m.position),'[]'::jsonb)
        from public.forward_schedule_stage_member_20261008 m
        where m.batch_id=b.batch_id and m.observation_digest is not null),
      'old_row_ids',(select coalesce(jsonb_agg(o.calendar_row_id order by o.position),'[]'::jsonb)
        from public.forward_schedule_stage_old_row_20261008 o where o.batch_id=b.batch_id),
      'finalize_receipt',b.finalize_receipt);
  end if;
  members:=payload->'members';
  for member in select value from jsonb_array_elements(members) loop
    pos:=pos+1;
    if jsonb_typeof(member) is distinct from 'object'
        or jsonb_typeof(member->'row') is distinct from 'object'
        or coalesce(jsonb_typeof(member->'observation'),'null') not in ('null','object') then
      raise exception 'exact stage batch membership required' using errcode='22023';
    end if;
    -- Only real content_calendar columns may be staged; unknown keys refuse.
    if exists(select 1 from jsonb_object_keys(member->'row') k
        where k not in (select a.attname from pg_catalog.pg_attribute a
          where a.attrelid='public.content_calendar'::regclass and a.attnum>0 and not a.attisdropped)) then
      raise exception 'staged calendar column unavailable' using errcode='22023';
    end if;
    select * into r from jsonb_populate_record(null::public.content_calendar, member->'row');
    if r.id is null or r.id=any(member_ids) or r.post_date is null or r.logical_post_id is null
        or nullif(btrim(r.gym_id),'') is null
        or r.status is null or r.status not in ('pending','draft')
        or (member->'row'->>'variant_status') is not null and (member->'row'->>'variant_status')<>'candidate'
        or (member->'row'->>'media_not_ready_reason') is not null
        or r.publish_claim_token is not null or r.publish_reservation_day is not null
        or r.published_at is not null or r.late_post_id is not null
        or r.source_media_url is null or r.source_media_url !~ '^https://[^[:space:]]+$'
        or r.image_url is null or r.image_url !~ '^https://[^[:space:]]+$'
        or (r.thumbnail_url is not null and r.thumbnail_url !~ '^https://[^[:space:]]+$') then
      raise exception 'inactive unapproved schedule candidate payload invalid' using errcode='23514';
    end if;
    select a.tenant_id into tenant from public.fixer_forward_media_tenant_alias_20261006 a
      where a.alias_key=btrim(r.gym_id);
    tenant:=coalesce(tenant,nullif(btrim(r.gym_id),''));
    if tenant is distinct from btrim(p_tenant_id) then
      raise exception 'batch tenant mismatch' using errcode='23514';
    end if;
    -- A calendar row already staged in another batch can never move.
    if exists(select 1 from public.forward_schedule_stage_member_20261008 m where m.calendar_row_id=r.id)
        or exists(select 1 from public.content_calendar c where c.id=r.id) then
      raise exception 'calendar row already staged' using errcode='23514';
    end if;
    r.variant_status:='candidate';
    r.media_not_ready_reason:='forward_reservation_staged';
    insert into public.content_calendar select r.*;
    select * into inserted from public.content_calendar where id=r.id;
    snapshot:=to_jsonb(inserted);
    revision:=md5(snapshot::text);
    digest:=null;
    obs:=member->'observation';
    if jsonb_typeof(obs)='object' then
      if nullif(btrim(coalesce(r.source_media_asset_id,'')),'') is null
          or obs->>'observation_json' is null or obs->>'digest_input' is null
          or octet_length(obs->>'observation_json')>65536 or octet_length(obs->>'digest_input')>65536 then
        raise exception 'bounded unverified observation required' using errcode='23514';
      end if;
      observation:=(obs->>'observation_json')::jsonb;
      digest:=encode(sha256(convert_to(obs->>'digest_input','UTF8')),'hex');
      if jsonb_typeof(observation) is distinct from 'object'
          or observation->'schema_version' is distinct from '1'::jsonb
          or observation->>'provenance_status' is distinct from 'unverified'
          or observation->'observation_digest' is distinct from to_jsonb(digest)
          or (observation-'observation_digest') is distinct from (obs->>'digest_input')::jsonb
          or jsonb_typeof(observation->'recipe') is distinct from 'object'
          or jsonb_typeof(observation->'hold_reasons') is distinct from 'array'
          or coalesce(observation->>'source_sha256','') !~ '^[0-9a-f]{64}$'
          or coalesce(observation->>'delivered_sha256','') !~ '^[0-9a-f]{64}$'
          or jsonb_typeof(observation->'source_byte_length') is distinct from 'number'
          or jsonb_typeof(observation->'delivered_byte_length') is distinct from 'number'
          or (observation->>'source_byte_length') !~ '^[1-9][0-9]{0,8}$'
          or (observation->>'delivered_byte_length') !~ '^[1-9][0-9]{0,8}$'
          or (observation->>'source_byte_length')::bigint>134217728
          or (observation->>'delivered_byte_length')::bigint>134217728 then
        raise exception 'unverified observation contract invalid' using errcode='23514';
      end if;
      if observation->>'tenant' is distinct from tenant
          or observation->>'source_asset_id' is distinct from r.source_media_asset_id
          or observation->>'source_exact_url' is distinct from r.source_media_url
          or observation->>'delivered_exact_url' is distinct from r.image_url then
        raise exception 'canonical observation media binding invalid' using errcode='23514';
      end if;
      insert into public.fixer_forward_media_observation_20261007
        (calendar_row_id,row_revision,observation_digest,tenant_id,gym_id,source_asset_id,
         source_exact_url,delivered_exact_url,calendar_snapshot,observation_json,digest_input)
        values(r.id,revision,digest,tenant,r.gym_id,r.source_media_asset_id,
          r.source_media_url,r.image_url,snapshot,obs->>'observation_json',obs->>'digest_input')
        on conflict(calendar_row_id,row_revision) do nothing;
      select * into existing from public.fixer_forward_media_observation_20261007
        where calendar_row_id=r.id and row_revision=revision;
      if existing.observation_digest is distinct from digest
          or existing.observation_json is distinct from (obs->>'observation_json')
          or existing.digest_input is distinct from (obs->>'digest_input') then
        raise exception 'immutable observation conflict' using errcode='23514';
      end if;
      observed_ids:=array_append(observed_ids,r.id);
    end if;
    insert into public.forward_schedule_stage_member_20261008
      (batch_id,position,calendar_row_id,logical_post_id,post_date,gym_id,tenant_id,
       source_media_url,image_url,thumbnail_url,observation_digest,staged_snapshot)
      values(b.batch_id,pos,r.id,r.logical_post_id,r.post_date,r.gym_id,tenant,
        r.source_media_url,r.image_url,r.thumbnail_url,digest,snapshot);
    member_ids:=array_append(member_ids,r.id);
  end loop;
  -- Persist the exact old-row CAS set. Identity pre-pass: unique, non-null,
  -- disjoint from the candidate members.
  for old_item in select value from jsonb_array_elements(payload->'old_rows') loop
    if jsonb_typeof(old_item) is distinct from 'object'
        or (old_item->>'id') is null then
      raise exception 'exact stage batch old row set required' using errcode='22023';
    end if;
    -- Only real content_calendar columns may appear; unknown keys refuse.
    if exists(select 1 from jsonb_object_keys(old_item) k
        where k not in (select a.attname from pg_catalog.pg_attribute a
          where a.attrelid='public.content_calendar'::regclass and a.attnum>0 and not a.attisdropped)) then
      raise exception 'staged calendar column unavailable' using errcode='22023';
    end if;
    if (old_item->>'id')::uuid=any(member_ids) or (old_item->>'id')::uuid=any(old_ids) then
      raise exception 'duplicate or missing calendar batch identity' using errcode='22023';
    end if;
    old_ids:=array_append(old_ids,(old_item->>'id')::uuid);
  end loop;
  -- Deterministic row order; census serializes all cooperating transactions.
  perform 1 from public.content_calendar where id=any(old_ids) order by id for update;
  for old_item in select value from jsonb_array_elements(payload->'old_rows') loop
    old_pos:=old_pos+1;
    select * into r from public.content_calendar where id=(old_item->>'id')::uuid;
    -- Full-column CAS against the live row; approved/queued/publishing/
    -- published, media-held, claimed, reserved or sent rows are protected.
    if not found or to_jsonb(r) is distinct from old_item or r.status is null or r.status not in ('pending','draft')
     or r.variant_status is distinct from 'active' or r.media_not_ready_reason is not null or r.publish_claim_token is not null
     or r.publish_reservation_day is not null or r.published_at is not null or r.late_post_id is not null then
     raise exception 'old calendar snapshot changed or protected' using errcode='23514'; end if;
    select a.tenant_id into tenant from public.fixer_forward_media_tenant_alias_20261006 a where a.alias_key=btrim(r.gym_id);
    tenant:=coalesce(tenant,btrim(r.gym_id));
    if tenant is distinct from b.tenant_id then raise exception 'batch tenant mismatch' using errcode='23514'; end if;
    -- Persist the authoritative live snapshot (equal to the caller bytes by
    -- the CAS above) as immutable durable evidence bound to this batch.
    insert into public.forward_schedule_stage_old_row_20261008
      (batch_id,position,calendar_row_id,tenant_id,logical_post_id,post_date,old_snapshot)
      values(b.batch_id,old_pos,r.id,tenant,r.logical_post_id,r.post_date,to_jsonb(r));
  end loop;
  return jsonb_build_object('batch_id',b.batch_id,'tenant_id',b.tenant_id,
    'request_digest',b.request_digest,'state','staged',
    'member_row_ids',(select coalesce(jsonb_agg(x.id order by x.ord),'[]'::jsonb)
      from unnest(member_ids) with ordinality x(id,ord)),
    'observation_row_ids',(select coalesce(jsonb_agg(x.id order by x.ord),'[]'::jsonb)
      from unnest(observed_ids) with ordinality x(id,ord)),
    'old_row_ids',(select coalesce(jsonb_agg(x.id order by x.ord),'[]'::jsonb)
      from unnest(old_ids) with ordinality x(id,ord)),
    'finalize_receipt',null);
end;
$$;
revoke all on function public.stage_forward_schedule_batch_20261008(text,uuid,text,text)
  from public,anon,authenticated,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant execute on function public.stage_forward_schedule_batch_20261008(text,uuid,text,text) to service_role;

-- Narrow PREPARATION-ONLY eligibility. Ordinary eligible ACTIVE rows keep
-- their existing lane; a staged row must be an exact registered member of a
-- NONTERMINAL batch with tenant match, unchanged content/media binding,
-- candidate state + exact stage marker, and no claim/send/approval. This
-- never authorizes publish/claim: the claim stack stays active-only.
create function public.forward_schedule_preparation_eligible_20261008(
  p_calendar_row_id uuid
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare
  r public.content_calendar%rowtype;
  m public.forward_schedule_stage_member_20261008%rowtype;
  b public.forward_schedule_stage_batch_20261008%rowtype;
  tenant text;
  function_state text;
begin
  if p_calendar_row_id is null then
    raise exception 'exact preparation eligibility binding required' using errcode='22023';
  end if;
  select * into r from public.content_calendar where id=p_calendar_row_id;
  if not found then
    return jsonb_build_object('eligible',false,'mode',null,'tenant_id',null,'batch_id',null,
      'reason','calendar row unavailable');
  end if;
  select a.tenant_id into tenant from public.fixer_forward_media_tenant_alias_20261006 a
    where a.alias_key=btrim(coalesce(r.gym_id,''));
  tenant:=coalesce(tenant,nullif(btrim(coalesce(r.gym_id,'')),''));
  -- Ordinary eligible active row: the pre-existing preparation lane.
  if r.variant_status='active' then
    function_state:=case
      when r.media_not_ready_reason is not null then 'media held'
      when r.status is null or r.status not in ('draft','pending','queued','approved') then 'status not preparable'
      when r.publish_claim_token is not null then 'publish claim exists'
      when r.published_at is not null or r.late_post_id is not null then 'already sent'
      when tenant is null then 'canonical tenant mapping unavailable'
      else null end;
    return jsonb_build_object('eligible',function_state is null,
      'mode',case when function_state is null then 'active' end,
      'tenant_id',tenant,'batch_id',null,'reason',function_state);
  end if;
  -- Staged member lane: exact registered nonterminal membership required.
  if r.variant_status is distinct from 'candidate'
      or r.media_not_ready_reason is distinct from 'forward_reservation_staged' then
    return jsonb_build_object('eligible',false,'mode',null,'tenant_id',tenant,'batch_id',null,
      'reason','not a staged schedule candidate');
  end if;
  select * into m from public.forward_schedule_stage_member_20261008 where calendar_row_id=r.id;
  if not found then
    return jsonb_build_object('eligible',false,'mode',null,'tenant_id',tenant,'batch_id',null,
      'reason','unregistered staged row');
  end if;
  select * into b from public.forward_schedule_stage_batch_20261008 where batch_id=m.batch_id;
  if not found or b.state is distinct from 'staged' then
    return jsonb_build_object('eligible',false,'mode',null,'tenant_id',tenant,'batch_id',m.batch_id,
      'reason','stage batch terminal or unavailable');
  end if;
  function_state:=case
    when tenant is null or tenant is distinct from m.tenant_id or tenant is distinct from b.tenant_id
      then 'tenant binding changed'
    when r.logical_post_id is distinct from m.logical_post_id
      or r.post_date is distinct from m.post_date or r.gym_id is distinct from m.gym_id
      or r.source_media_url is distinct from m.source_media_url
      or r.image_url is distinct from m.image_url
      or r.thumbnail_url is distinct from m.thumbnail_url then 'content/media binding changed'
    when r.status is null or r.status not in ('pending','draft') then 'candidate status not preparable'
    when r.publish_claim_token is not null then 'publish claim exists'
    when r.published_at is not null or r.late_post_id is not null then 'already sent'
    -- Full-row binding against the persisted staged snapshot: every column
    -- must equal the staged bytes except the trusted preparation output
    -- fields (source_media_asset_id, render_manifest_digest) written by the
    -- isolated owner/attester preparation path. Any caller edit to planned
    -- content/media/tenant fields (account, format, caption,
    -- visual_group_key, dates, URLs, ...) fails closed here.
    when (to_jsonb(r) - 'source_media_asset_id' - 'render_manifest_digest')
         is distinct from (m.staged_snapshot - 'source_media_asset_id' - 'render_manifest_digest')
      then 'staged snapshot changed'
    else null end;
  return jsonb_build_object('eligible',function_state is null,
    'mode',case when function_state is null then 'staged' end,
    'tenant_id',tenant,'batch_id',m.batch_id,'reason',function_state);
end;
$$;
revoke all on function public.forward_schedule_preparation_eligible_20261008(uuid)
  from public,anon,authenticated;
grant execute on function public.forward_schedule_preparation_eligible_20261008(uuid)
  to service_role,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006;

-- Batch-ID finalizer. Re-checks the COMPLETE persisted membership and uses
-- ONLY the PERSISTED old-row set: a caller old-row set that differs from the
-- persisted snapshots is refused, every persisted old row is CAS re-validated
-- against its live row (a change after staging holds the batch), and only the
-- persisted old IDs are archived (a row added after staging is never
-- archived). Then runs the legacy activation/reservation/old-row-archive
-- sequence and writes the terminal receipt in the SAME transaction. Exact
-- retry returns the persisted receipt; a changed request against a finalized
-- batch refuses.
create function public.finalize_forward_schedule_staged_batch_20261008(
  p_tenant_id text,p_batch_id uuid,p_candidates jsonb,p_expected_old_rows jsonb
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare
  b public.forward_schedule_stage_batch_20261008%rowtype;
  m public.forward_schedule_stage_member_20261008%rowtype;
  item jsonb; old_snapshot jsonb; r public.content_calendar%rowtype; tenant text;
  candidates uuid[]; member_ids uuid[]; old_ids uuid[]; old_snapshots jsonb[];
  row_ids uuid[]:=array[]::uuid[]; reservations uuid[]:=array[]::uuid[];
  reservation uuid; ids uuid[]; expected uuid;
  v_finalize_request jsonb; receipt jsonb;
begin
  if nullif(btrim(p_tenant_id),'') is null or p_batch_id is null
      or jsonb_typeof(p_candidates) is distinct from 'array'
      or jsonb_array_length(p_candidates)=0
      or jsonb_typeof(p_expected_old_rows) is distinct from 'array' then
    raise exception 'exact schedule batch binding required' using errcode='22023';
  end if;
  if current_setting('transaction_isolation')<>'read committed' then
    raise exception 'forward media authority requires read committed isolation' using errcode='25000';
  end if;
  perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
  if not exists(select 1 from public.forward_schedule_reservation_gate_20261008 where singleton and enabled) then
    raise exception 'schedule reservation authority is OFF pending review' using errcode='55000';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
  select * into b from public.forward_schedule_stage_batch_20261008
    where batch_id=p_batch_id for update;
  if not found then
    raise exception 'schedule batch unavailable' using errcode='23514';
  end if;
  if b.tenant_id is distinct from btrim(p_tenant_id) then
    raise exception 'batch tenant mismatch' using errcode='23514';
  end if;
  v_finalize_request:=jsonb_build_object('candidates',p_candidates,'expected_old_rows',p_expected_old_rows);
  if b.state='finalized' then
    if b.finalize_request is distinct from v_finalize_request then
      raise exception 'finalized schedule batch request changed' using errcode='23514';
    end if;
    return b.finalize_receipt;
  end if;
  -- The PERSISTED old-row set is the sole finalization authority. The caller
  -- must present exactly that set (proving it read back the staged batch);
  -- any substitution, omission or addition is refused before any write.
  select coalesce(array_agg(o.calendar_row_id order by o.position),array[]::uuid[]),
         coalesce(array_agg(o.old_snapshot order by o.position),array[]::jsonb[])
    into old_ids, old_snapshots
    from public.forward_schedule_stage_old_row_20261008 o where o.batch_id=b.batch_id;
  if (select coalesce(jsonb_agg(v order by v),'[]'::jsonb) from jsonb_array_elements(p_expected_old_rows) v)
      is distinct from
     (select coalesce(jsonb_agg(s order by s),'[]'::jsonb) from unnest(old_snapshots) s) then
    raise exception 'staged old row set mismatch' using errcode='23514';
  end if;
  select coalesce(array_agg(x.calendar_row_id),array[]::uuid[]) into member_ids
    from public.forward_schedule_stage_member_20261008 x where x.batch_id=b.batch_id;
  select array_agg((v->>'calendar_row_id')::uuid) into candidates from jsonb_array_elements(p_candidates) v;
  if candidates is null or array_position(candidates,null) is not null
      or not (candidates <@ member_ids and member_ids <@ candidates) then
    raise exception 'incomplete staged batch membership' using errcode='23514';
  end if;
  if exists(select 1 from unnest(candidates||old_ids) id group by id having count(*)>1)
      or array_position(old_ids,null) is not null then
    raise exception 'duplicate or missing calendar batch identity' using errcode='22023';
  end if;
  -- Deterministic row order; census serializes all cooperating transactions.
  perform 1 from public.content_calendar where id=any(candidates||old_ids) order by id for update;
  -- CAS re-validation uses ONLY the persisted snapshots: any old row changed
  -- after staging (approval, media edit, claim, send) holds the whole batch.
  for old_snapshot in select s from unnest(old_snapshots) s loop
    select * into r from public.content_calendar where id=(old_snapshot->>'id')::uuid;
    if not found or to_jsonb(r) is distinct from old_snapshot or r.status is null or r.status not in ('pending','draft')
     or r.variant_status is distinct from 'active' or r.media_not_ready_reason is not null or r.publish_claim_token is not null
     or r.publish_reservation_day is not null or r.published_at is not null or r.late_post_id is not null then
     raise exception 'old calendar snapshot changed or protected' using errcode='23514'; end if;
    select a.tenant_id into tenant from public.fixer_forward_media_tenant_alias_20261006 a where a.alias_key=btrim(r.gym_id);
    tenant:=coalesce(tenant,btrim(r.gym_id));
    if tenant is distinct from b.tenant_id then raise exception 'batch tenant mismatch' using errcode='23514'; end if;
  end loop;
  for item in select value from jsonb_array_elements(p_candidates) loop
    select * into r from public.content_calendar where id=(item->>'calendar_row_id')::uuid;
    select * into m from public.forward_schedule_stage_member_20261008 where calendar_row_id=r.id;
    if not found or r.variant_status is distinct from 'candidate' or r.media_not_ready_reason is distinct from 'forward_reservation_staged' or r.status is null or r.status not in ('pending','draft')
     or r.publish_claim_token is not null or r.published_at is not null or r.late_post_id is not null then
     raise exception 'inactive unapproved schedule candidate required' using errcode='23514'; end if;
    -- Immutable staged content/media binding must match exactly; m was found
    -- because candidates <@ member_ids was validated above.
    if m.logical_post_id is distinct from (item->>'logical_post_id')::uuid
     or r.logical_post_id is distinct from m.logical_post_id
     or r.post_date is distinct from m.post_date or r.gym_id is distinct from m.gym_id
     or r.source_media_url is distinct from m.source_media_url
     or r.image_url is distinct from m.image_url
     or r.thumbnail_url is distinct from m.thumbnail_url then
     raise exception 'staged member binding changed' using errcode='23514'; end if;
    -- Full-row binding against the persisted staged snapshot: a caller edit
    -- to ANY planned content/media/tenant field after staging (account,
    -- format, caption, visual_group_key, dates, URLs, ...) refuses the whole
    -- batch before activation. Only the trusted preparation output fields
    -- (source_media_asset_id, render_manifest_digest) may differ from the
    -- staged bytes; the reservation stack re-validates those against the
    -- owner/attester lineage, so a forged value cannot pass.
    if (to_jsonb(r) - 'source_media_asset_id' - 'render_manifest_digest')
       is distinct from (m.staged_snapshot - 'source_media_asset_id' - 'render_manifest_digest') then
     raise exception 'staged member snapshot changed' using errcode='23514'; end if;
    if m.tenant_id is distinct from b.tenant_id then raise exception 'batch tenant mismatch' using errcode='23514'; end if;
    ids:=array(select jsonb_array_elements_text(item->'attestation_ids'))::uuid[];
    expected:=nullif(item->>'expected_reservation_id','')::uuid;
    update public.content_calendar set variant_status='active',media_not_ready_reason=null where id=r.id;
    reservation:=public.reserve_forward_slot_20261008(r.id,(item->>'logical_post_id')::uuid,item->>'expected_revision',ids,expected);
    row_ids:=array_append(row_ids,r.id);
    reservations:=array_append(reservations,reservation);
  end loop;
  -- No old rows change until ALL candidates have passed. Only the persisted
  -- old IDs are archived; a row added after staging is never touched. The
  -- entire function is a transaction, so any following constraint/trigger
  -- failure also rolls back.
  update public.content_calendar set variant_status='archived' where id=any(old_ids);
  for reservation in select x.reservation_id from public.forward_schedule_reservation x
   where x.state='active' and x.calendar_row_id=any(old_ids) and not (x.reservation_id=any(reservations)) loop
   perform public.release_forward_slot_20261008(reservation,'atomic-calendar-replacement');
  end loop;
  receipt:=jsonb_build_object('batch_id',b.batch_id,'tenant_id',b.tenant_id,
    'request_digest',b.request_digest,'state','finalized',
    'row_ids',row_ids,'reservation_ids',reservations,'archived_old_row_ids',old_ids);
  update public.forward_schedule_stage_batch_20261008
    set state='finalized',finalize_request=v_finalize_request,finalize_receipt=receipt,finalized_at=now()
    where batch_id=b.batch_id and state='staged';
  return receipt;
end;
$$;
revoke all on function public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)
  from public,anon,authenticated,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant execute on function public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb) to service_role;

-- Read-only lost-response readback. The persisted receipt is the ONLY
-- resolution of an ambiguous stage/finalize outcome; this never writes and
-- never infers success from mutable calendar rows.
create function public.forward_schedule_batch_status_20261008(
  p_batch_id uuid
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare b public.forward_schedule_stage_batch_20261008%rowtype;
begin
  if p_batch_id is null then
    raise exception 'exact schedule batch binding required' using errcode='22023';
  end if;
  select * into b from public.forward_schedule_stage_batch_20261008 where batch_id=p_batch_id;
  if not found then
    raise exception 'schedule batch unavailable' using errcode='23514';
  end if;
  return jsonb_build_object('batch_id',b.batch_id,'tenant_id',b.tenant_id,
    'request_digest',b.request_digest,'state',b.state,
    'member_row_ids',(select coalesce(jsonb_agg(m.calendar_row_id order by m.position),'[]'::jsonb)
      from public.forward_schedule_stage_member_20261008 m where m.batch_id=b.batch_id),
    'observation_row_ids',(select coalesce(jsonb_agg(m.calendar_row_id order by m.position),'[]'::jsonb)
      from public.forward_schedule_stage_member_20261008 m
      where m.batch_id=b.batch_id and m.observation_digest is not null),
    'old_row_ids',(select coalesce(jsonb_agg(o.calendar_row_id order by o.position),'[]'::jsonb)
      from public.forward_schedule_stage_old_row_20261008 o where o.batch_id=b.batch_id),
    'finalize_receipt',b.finalize_receipt);
end;
$$;
revoke all on function public.forward_schedule_batch_status_20261008(uuid)
  from public,anon,authenticated,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant execute on function public.forward_schedule_batch_status_20261008(uuid) to service_role;

commit;
