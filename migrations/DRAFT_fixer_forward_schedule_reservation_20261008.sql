-- DRAFT / UNAPPLIED / DEFAULT OFF. No production activation is authorized.
-- Requires DRAFT_fixer_forward_media_claim_20261006.sql (base claim stack),
-- DRAFT_fixer_forward_visual_index_20261008.sql (visual attestation/negative/
-- proof authority) and the committed logical_post_id_20261004.sql column.
-- This draft EXTENDS that stack with durable FUTURE slot reservations keyed by
-- (tenant, post_date, logical_post_id). It never replaces, bypasses or
-- re-implements the visual claim, byte occupancy, approval or history
-- authority: committed claims remain the sole publication occupancy, and this
-- layer only consults them. Lock order (matches the existing stack):
-- graph -> census -> row -> slot. No locks are held across object fetches:
-- this migration performs no network/object I/O. Unknown historical inventory
-- HOLDS, never counts as clearance or exhaustion. Reservations are durable:
-- no delete; state transitions one-way active -> released|superseded|revoked;
-- terminal rows persist as occupancy evidence. A reservation is never a
-- ready-row or claim bypass: publishing/published rows cannot be reserved,
-- and final publication must still pass the full visual claim stack.
-- Rollback before use: remove the new objects. After use preserve all rows.
begin;

-- Hard prerequisites: this draft extends the visual index stack and the
-- validated logical post identity. Fail the whole migration if either is
-- absent.
do $$
begin
  if not exists(select 1 from pg_catalog.pg_proc p
    join pg_catalog.pg_namespace n on n.oid=p.pronamespace
    where n.nspname='public' and p.proname='fixer_forward_visual_index_claim_20261008') then
    raise exception 'forward visual index draft is required' using errcode='23514';
  end if;
  if not exists(select 1 from pg_catalog.pg_attribute
    where attrelid='public.content_calendar'::regclass and attname='logical_post_id' and not attisdropped) then
    raise exception 'content_calendar.logical_post_id is required' using errcode='23514';
  end if;
end;
$$;

-- Protected singleton activation gate, default OFF. Gate writes serialize
-- with claims/reserves through the exclusive graph lock.
create table public.forward_schedule_reservation_gate_20261008 (
  singleton boolean primary key default true check(singleton),
  enabled boolean not null default false
);
insert into public.forward_schedule_reservation_gate_20261008 values(true,false);
alter table public.forward_schedule_reservation_gate_20261008 enable row level security;
revoke all on public.forward_schedule_reservation_gate_20261008
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
create trigger reservation_gate_lock before insert or update or delete on public.forward_schedule_reservation_gate_20261008
  for each row execute function public.fixer_forward_visual_gate_lock_20261008();

-- Durable future slot reservations. One ACTIVE reservation per
-- (tenant, post_date, logical_post_id). Terminal rows persist as occupancy
-- evidence; there is no delete path.
create table public.forward_schedule_reservation (
  reservation_id uuid primary key default gen_random_uuid(),
  tenant_id text not null check (tenant_id=btrim(tenant_id) and tenant_id<>''),
  calendar_row_id uuid not null,
  logical_post_id uuid not null,
  post_date date not null,
  source_asset_id text not null check (source_asset_id=btrim(source_asset_id) and source_asset_id<>''),
  source_url text not null check (source_url ~ '^https://[^[:space:]]+$'),
  source_sha256 text not null check (source_sha256 ~ '^[0-9a-f]{64}$'),
  phash_version integer not null default 1 check (phash_version=1),
  phash_v1 bigint not null,
  row_revision text not null check (btrim(row_revision)<>''),
  lineage_evidence_id uuid not null references public.fixer_forward_media_lineage_20261006(evidence_id),
  attestation_ids uuid[] not null check(cardinality(attestation_ids)=3),
  state text not null default 'active' check (state in ('active','released','superseded','revoked')),
  state_reason text check (state_reason is null or btrim(state_reason)<>''),
  created_at timestamptz not null default now(),
  state_changed_at timestamptz
);
create unique index forward_schedule_reservation_active_slot
  on public.forward_schedule_reservation(tenant_id,post_date,logical_post_id) where state='active';
create index forward_schedule_reservation_sha_20261008
  on public.forward_schedule_reservation(source_sha256);
create index forward_schedule_reservation_phash_20261008
  on public.forward_schedule_reservation(phash_v1) where state='active';
create index forward_schedule_reservation_slot_20261008
  on public.forward_schedule_reservation(tenant_id,post_date,logical_post_id);

-- Write guard: only the SECURITY DEFINER reservation RPCs (whose owner is
-- the migration owner) may write; transitions are one-way and proof columns
-- are immutable once written. SECURITY INVOKER keeps the effective owner
-- inside the definer RPCs but sees the real role on direct DML.
create function public.fixer_forward_reservation_guard_20261008()
returns trigger language plpgsql security invoker set search_path=pg_catalog,public as $$
begin
  if current_user::regrole::oid is distinct from (
    select p.proowner from pg_catalog.pg_proc p
    where p.oid='public.reserve_forward_slot_20261008(uuid,uuid,text,uuid[],uuid)'::regprocedure
  ) then
    raise exception 'forward schedule reservations are RPC-managed only' using errcode='42501';
  end if;
  if tg_op='DELETE' then
    raise exception 'forward schedule reservations are durable; deletion forbidden' using errcode='23514';
  end if;
  if tg_op='INSERT' and new.state is distinct from 'active' then
    raise exception 'forward schedule reservations are inserted active only' using errcode='23514';
  end if;
  if tg_op='UPDATE' then
    if old.state is distinct from 'active' or new.state='active'
        or new.reservation_id is distinct from old.reservation_id
        or new.tenant_id is distinct from old.tenant_id
        or new.calendar_row_id is distinct from old.calendar_row_id
        or new.logical_post_id is distinct from old.logical_post_id
        or new.post_date is distinct from old.post_date
        or new.source_asset_id is distinct from old.source_asset_id
        or new.source_url is distinct from old.source_url
        or new.source_sha256 is distinct from old.source_sha256
        or new.phash_version is distinct from old.phash_version
        or new.phash_v1 is distinct from old.phash_v1
        or new.row_revision is distinct from old.row_revision
        or new.lineage_evidence_id is distinct from old.lineage_evidence_id
        or new.attestation_ids is distinct from old.attestation_ids
        or new.created_at is distinct from old.created_at then
      raise exception 'forward schedule reservation proof is immutable; only one-way state transitions allowed' using errcode='23514';
    end if;
  end if;
  return coalesce(new,old);
end;
$$;
revoke all on function public.fixer_forward_reservation_guard_20261008()
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
create trigger reservation_guard before insert or update or delete on public.forward_schedule_reservation
  for each row execute function public.fixer_forward_reservation_guard_20261008();
create trigger reservation_guard_truncate before truncate on public.forward_schedule_reservation
  for each statement execute function public.fixer_forward_media_immutable_20261006();
alter table public.forward_schedule_reservation enable row level security;
revoke all on public.forward_schedule_reservation
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant select on public.forward_schedule_reservation to service_role;
create policy service_read on public.forward_schedule_reservation
  for select to service_role using(true);

-- Atomic future-slot reservation. Validates tenant, local date, logical post,
-- proof, CURRENT source/clearance/revision; consults committed visual claims,
-- historical negatives and existing future reservations; inserts or
-- CAS-replaces one durable reservation in this ONE transaction.
create function public.reserve_forward_slot_20261008(
  p_calendar_row_id uuid,p_logical_post_id uuid,p_expected_revision text,
  p_attestation_ids uuid[],p_expected_reservation_id uuid default null
) returns uuid language plpgsql security definer
set search_path=pg_catalog,public as $$
declare
  r public.content_calendar%rowtype;
  tenant text;
  base_revision text;
  expected_revision bigint;
  att record;
  m record;
  expected_url text;
  dist integer;
  evidence uuid;
  seen_roles text[]:=array[]::text[];
  src_sha text;
  src_phash bigint;
  slot public.forward_schedule_reservation%rowtype;
  ident uuid;
begin
  if p_calendar_row_id is null or p_logical_post_id is null
      or nullif(btrim(p_expected_revision),'') is null or p_attestation_ids is null
      or cardinality(p_attestation_ids)<>3 then
    raise exception 'exact reservation binding required' using errcode='22023';
  end if;
  if current_setting('transaction_isolation')<>'read committed' then
    raise exception 'forward media authority requires read committed isolation' using errcode='25000';
  end if;
  -- Lock order: graph -> census -> row -> slot (matches the claim stack).
  perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
  if not exists(select 1 from public.forward_schedule_reservation_gate_20261008 where singleton and enabled) then
    raise exception 'schedule reservation authority is OFF pending review' using errcode='55000';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
  -- A reservation is never a ready-row or claim bypass: only unsent,
  -- unclaimed, planned rows may reserve a future slot.
  select * into r from public.content_calendar where id=p_calendar_row_id for update;
  if not found or r.status is null or r.status not in ('draft','pending','queued','approved')
      or r.publish_claim_token is not null
      or r.published_at is not null or r.late_post_id is not null
      or r.variant_status is distinct from 'active'
      or r.media_not_ready_reason is not null
      or r.post_date is null
      or nullif(btrim(r.gym_id),'') is null then
    raise exception 'reservation requires an unsent planned calendar row' using errcode='23514';
  end if;
  -- The persisted validated logical post is authoritative; the caller cannot
  -- invent or borrow one. visual_group_key remains a separate concept.
  if r.logical_post_id is null or r.logical_post_id is distinct from p_logical_post_id then
    raise exception 'logical post binding invalid' using errcode='23514';
  end if;
  select a.tenant_id into tenant from public.fixer_forward_media_tenant_alias_20261006 a
    where a.alias_key=btrim(r.gym_id);
  tenant:=coalesce(tenant,nullif(btrim(r.gym_id),''));
  if tenant is null then raise exception 'canonical tenant mapping unavailable' using errcode='23514'; end if;
  -- Bind the exact persisted outgoing media revision. Changed media
  -- invalidates the reservation request; approval is never inherited after
  -- changed media.
  base_revision:=public.fixer_forward_media_attestation_request_20261006(r.id)->>'revision';
  expected_revision:=('x'||substr(base_revision,1,15))::bit(60)::bigint;
  if base_revision is distinct from p_expected_revision then
    raise exception 'outgoing media revision changed' using errcode='23514';
  end if;
  -- Serialize same-slot reserves AFTER the row lock to keep the global order.
  perform pg_advisory_xact_lock(hashtextextended(
    jsonb_build_array('fixer_forward_slot_20261008',tenant,r.post_date,p_logical_post_id)::text,0));
  -- CURRENT source authority: owner registry tuple must match the row, and
  -- the exact tuple must hold cleared_unused with no fleet hold for its
  -- fingerprint. Unknown history holds; an empty index grants no clearance.
  perform public.fixer_forward_media_provenance_lookup_20261006(r.id);
  -- Revocation fence: a revoked reservation makes its (tenant, source asset)
  -- terminal under this draft. Reinstatement requires a fresh source identity
  -- with fresh owner clearance, never a reservation overwrite.
  if exists(select 1 from public.forward_schedule_reservation x
      where x.tenant_id=tenant and x.source_asset_id=r.source_media_asset_id and x.state='revoked') then
    raise exception 'reservation source revoked; fresh source identity and clearance required' using errcode='23514';
  end if;
  -- Verify every attestation against trusted persisted evidence: exactly the
  -- three roles, this tenant, the current revision, one shared lineage,
  -- exact URLs and trusted object-read receipts (md5 + length).
  for att in select a.* from public.forward_media_visual_attestation a
    where a.attestation_id=any(p_attestation_ids) loop
    if att.tenant_key is distinct from tenant
        or att.row_revision is distinct from expected_revision
        or att.role=any(seen_roles)
        or (evidence is not null and att.lineage_receipt_id is distinct from evidence) then
      raise exception 'reservation attestation evidence invalid' using errcode='23514';
    end if;
    evidence:=att.lineage_receipt_id;
    seen_roles:=array_append(seen_roles,att.role);
    expected_url:=case att.role
      when 'original' then r.source_media_url
      when 'delivered' then r.image_url
      else coalesce(r.thumbnail_url,r.image_url) end;
    if expected_url is null or att.media_url is distinct from expected_url then
      raise exception 'reservation attestation evidence invalid' using errcode='23514';
    end if;
    if not exists(select 1 from public.fixer_forward_media_object_read_20261006 o
        where o.receipt_id=att.object_read_receipt_id and o.tenant_id=tenant
          and o.exact_url=att.media_url and o.fingerprint='md5:'||att.source_md5
          and o.byte_length=att.byte_length
          and o.receipt_id=(select case att.role when 'original' then l.source_read_receipt
              when 'delivered' then l.image_read_receipt
              else coalesce(l.thumbnail_read_receipt,l.image_read_receipt) end
            from public.fixer_forward_media_lineage_20261006 l where l.evidence_id=evidence)) then
      raise exception 'reservation attestation evidence invalid' using errcode='23514';
    end if;
    if att.role='original' then
      src_sha:=att.source_sha256;
      src_phash:=att.phash_v1;
    end if;
  end loop;
  if cardinality(seen_roles)<>3 or src_sha is null then
    raise exception 'reservation attestation evidence invalid' using errcode='23514';
  end if;
  if not exists(select 1 from public.fixer_forward_media_lineage_20261006 l
      where l.evidence_id=evidence and l.calendar_row_id=r.id and l.row_revision=base_revision
        and l.tenant_id=tenant and l.group_key=r.visual_group_key
        and l.source_asset_id=r.source_media_asset_id and l.manifest_digest=r.render_manifest_digest) then
    raise exception 'reservation lineage binding invalid' using errcode='23514';
  end if;
  -- Late negative evidence blocks reservation: exact SHA or pHash <= 30.
  if exists(select 1 from public.forward_media_visual_negative n
      where n.source_sha256=src_sha
        or (n.phash_version=1
          and length(replace(((src_phash # n.phash_v1)::bit(64))::text,'0',''))<=30)) then
    raise exception 'visual negative evidence blocks reservation' using errcode='23514';
  end if;
  -- Committed visual occupancy (claims): equal SHA is reusable ONLY by exact
  -- same tenant/date/logical-post siblings (the receipt's calendar row must
  -- still carry the same validated logical_post_id; a deleted or unknown row
  -- fails closed). Any other tenant, date or logical post blocks. pHash <=6
  -- blocks cross logical post, 7-30 holds for review, >30 no match.
  for m in select rec.tenant_id, rec.post_date, rec.calendar_row_id
      from public.forward_media_visual_attestation b
      join public.forward_media_visual_claim_proof_20261008 proof on b.attestation_id=any(proof.attestation_ids)
      join public.fixer_forward_media_claim_receipt_20261006 rec on rec.claim_token=proof.claim_token
      where b.source_sha256=src_sha loop
    if m.tenant_id is distinct from tenant or m.post_date is distinct from r.post_date
        or not exists(select 1 from public.content_calendar cr
          where cr.id=m.calendar_row_id and cr.logical_post_id=p_logical_post_id) then
      raise exception 'visual byte ancestry already consumed by another tenant/date/logical post' using errcode='23514';
    end if;
  end loop;
  for m in select b.phash_v1, rec.tenant_id, rec.post_date, rec.calendar_row_id
      from public.forward_media_visual_attestation b
      join public.forward_media_visual_claim_proof_20261008 proof on b.attestation_id=any(proof.attestation_ids)
      join public.fixer_forward_media_claim_receipt_20261006 rec on rec.claim_token=proof.claim_token
      where b.phash_version=1 and b.source_sha256 is distinct from src_sha loop
    dist:=length(replace(((src_phash # m.phash_v1)::bit(64))::text,'0',''));
    if dist>30 then continue; end if;
    if m.tenant_id is not distinct from tenant and m.post_date is not distinct from r.post_date
        and exists(select 1 from public.content_calendar cr
          where cr.id=m.calendar_row_id and cr.logical_post_id=p_logical_post_id) then continue; end if;
    if dist<=6 then
      raise exception 'visual byte ancestry already consumed by another tenant/date/logical post' using errcode='23514';
    end if;
    raise exception 'visual similarity held for review' using errcode='23514';
  end loop;
  -- Existing FUTURE reservations on other slots: equal SHA blocks; pHash
  -- policy identical to committed occupancy. Own slot is excluded here and
  -- resolved below.
  for m in select x.reservation_id, x.phash_v1 from public.forward_schedule_reservation x
      where x.state='active' and x.source_sha256=src_sha
        and (x.tenant_id is distinct from tenant or x.post_date is distinct from r.post_date
          or x.logical_post_id is distinct from p_logical_post_id) loop
    raise exception 'source already reserved for another tenant/date/logical post' using errcode='23514';
  end loop;
  for m in select x.reservation_id, x.phash_v1 from public.forward_schedule_reservation x
      where x.state='active' and x.source_sha256 is distinct from src_sha
        and (x.tenant_id is distinct from tenant or x.post_date is distinct from r.post_date
          or x.logical_post_id is distinct from p_logical_post_id) loop
    dist:=length(replace(((src_phash # m.phash_v1)::bit(64))::text,'0',''));
    if dist>30 then continue; end if;
    if dist<=6 then
      raise exception 'visual byte ancestry already consumed by another tenant/date' using errcode='23514';
    end if;
    raise exception 'visual similarity held for review' using errcode='23514';
  end loop;
  -- Slot resolution under the slot advisory lock; the partial unique index is
  -- the final backstop so at most one active reservation exists per slot.
  select * into slot from public.forward_schedule_reservation x
    where x.state='active' and x.tenant_id=tenant and x.post_date=r.post_date
      and x.logical_post_id=p_logical_post_id;
  if found then
    -- Idempotent exact same-slot retry / same-day sibling reuse of its own
    -- visual: the caller's proof for THIS row was fully validated above, so
    -- an identical source on the same slot returns the existing reservation
    -- (sibling rows carry their own per-row revisions and attestation ids).
    if slot.source_sha256=src_sha then
      return slot.reservation_id;
    end if;
    if p_expected_reservation_id is null or p_expected_reservation_id is distinct from slot.reservation_id then
      raise exception 'schedule reservation slot conflict' using errcode='23514';
    end if;
    -- CAS replacement: supersede the expected incumbent and insert the new
    -- active reservation atomically in this transaction. The superseded row
    -- persists as occupancy evidence.
    update public.forward_schedule_reservation
      set state='superseded', state_reason='cas-replaced', state_changed_at=now()
      where reservation_id=slot.reservation_id and state='active';
    if not found then
      raise exception 'schedule reservation slot conflict' using errcode='23514';
    end if;
  end if;
  insert into public.forward_schedule_reservation
    (tenant_id,calendar_row_id,logical_post_id,post_date,source_asset_id,source_url,
     source_sha256,phash_v1,row_revision,lineage_evidence_id,attestation_ids)
    values(tenant,r.id,p_logical_post_id,r.post_date,r.source_media_asset_id,r.source_media_url,
      src_sha,src_phash,base_revision,evidence,p_attestation_ids)
    returning reservation_id into ident;
  return ident;
end;
$$;
revoke all on function public.reserve_forward_slot_20261008(uuid,uuid,text,uuid[],uuid)
  from public,anon,authenticated,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant execute on function public.reserve_forward_slot_20261008(uuid,uuid,text,uuid[],uuid) to service_role;

-- Release frees the slot for another source. Idempotent on terminal rows;
-- never deletes. Callable while the gate is OFF (it only reduces occupancy).
create function public.release_forward_slot_20261008(
  p_reservation_id uuid,p_reason text
) returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare x public.forward_schedule_reservation%rowtype;
begin
  if p_reservation_id is null or nullif(btrim(p_reason),'') is null then
    raise exception 'exact reservation release binding required' using errcode='22023';
  end if;
  if current_setting('transaction_isolation')<>'read committed' then
    raise exception 'forward media authority requires read committed isolation' using errcode='25000';
  end if;
  select * into x from public.forward_schedule_reservation where reservation_id=p_reservation_id for update;
  if not found then
    raise exception 'schedule reservation unavailable' using errcode='23514';
  end if;
  if x.state is distinct from 'active' then
    return true;
  end if;
  update public.forward_schedule_reservation
    set state='released', state_reason=p_reason, state_changed_at=now()
    where reservation_id=x.reservation_id and state='active';
  return true;
end;
$$;
revoke all on function public.release_forward_slot_20261008(uuid,text)
  from public,anon,authenticated,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant execute on function public.release_forward_slot_20261008(uuid,text) to service_role;

-- Tenant-scoped source revocation: every active reservation for the source
-- becomes terminally revoked. Returns the revoked count (0 allowed).
create function public.revoke_source_reservations_20261008(
  p_tenant_id text,p_source_sha256 text,p_reason text
) returns integer language plpgsql security definer set search_path=pg_catalog,public as $$
declare n integer;
begin
  if nullif(btrim(p_tenant_id),'') is null or p_source_sha256 is null
      or p_source_sha256 !~ '^[0-9a-f]{64}$' or nullif(btrim(p_reason),'') is null then
    raise exception 'exact source revocation binding required' using errcode='22023';
  end if;
  if current_setting('transaction_isolation')<>'read committed' then
    raise exception 'forward media authority requires read committed isolation' using errcode='25000';
  end if;
  update public.forward_schedule_reservation
    set state='revoked', state_reason=p_reason, state_changed_at=now()
    where state='active' and tenant_id=btrim(p_tenant_id) and source_sha256=p_source_sha256;
  get diagnostics n=row_count;
  return n;
end;
$$;
revoke all on function public.revoke_source_reservations_20261008(text,text,text)
  from public,anon,authenticated,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant execute on function public.revoke_source_reservations_20261008(text,text,text) to service_role;

-- Read-only ADVISORY conflict screen for planner candidate search. Takes no
-- locks and grants no authority: only reserve_forward_slot_20261008 admits
-- occupancy. A bounded search that stops early proves nothing about
-- depletion.
create function public.check_reservation_conflicts_20261008(
  p_tenant_id text,p_post_date date,p_logical_post_id uuid,p_source_sha256 text,p_phash_v1 bigint
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare conflicts jsonb:= '[]'::jsonb;
begin
  if nullif(btrim(p_tenant_id),'') is null or p_post_date is null or p_logical_post_id is null
      or p_source_sha256 is null or p_source_sha256 !~ '^[0-9a-f]{64}$' or p_phash_v1 is null then
    raise exception 'exact conflict screening binding required' using errcode='22023';
  end if;
  select conflicts||coalesce(jsonb_agg(jsonb_build_object('kind','negative')), '[]'::jsonb) into conflicts
    from public.forward_media_visual_negative n
    where n.source_sha256=p_source_sha256
      or (n.phash_version=1 and length(replace(((p_phash_v1 # n.phash_v1)::bit(64))::text,'0',''))<=30);
  select conflicts||coalesce(jsonb_agg(jsonb_build_object('kind',
        case when b.source_sha256=p_source_sha256 then 'committed_claim'
             when length(replace(((p_phash_v1 # b.phash_v1)::bit(64))::text,'0',''))<=6 then 'phash_block'
             else 'phash_review' end,
        'claim_token',rec.claim_token,'tenant_id',rec.tenant_id,'post_date',rec.post_date,
        'distance',length(replace(((p_phash_v1 # b.phash_v1)::bit(64))::text,'0','')))), '[]'::jsonb) into conflicts
    from public.forward_media_visual_attestation b
    join public.forward_media_visual_claim_proof_20261008 proof on b.attestation_id=any(proof.attestation_ids)
    join public.fixer_forward_media_claim_receipt_20261006 rec on rec.claim_token=proof.claim_token
    where (rec.tenant_id is distinct from p_tenant_id or rec.post_date is distinct from p_post_date
        or not exists(select 1 from public.content_calendar cr
          where cr.id=rec.calendar_row_id and cr.logical_post_id=p_logical_post_id))
      and (b.source_sha256=p_source_sha256
        or (b.phash_version=1 and b.source_sha256 is distinct from p_source_sha256
          and length(replace(((p_phash_v1 # b.phash_v1)::bit(64))::text,'0',''))<=30));
  select conflicts||coalesce(jsonb_agg(jsonb_build_object('kind',
        case when x.source_sha256=p_source_sha256 then 'active_reservation'
             when length(replace(((p_phash_v1 # x.phash_v1)::bit(64))::text,'0',''))<=6 then 'phash_block'
             else 'phash_review' end,
        'reservation_id',x.reservation_id,'tenant_id',x.tenant_id,'post_date',x.post_date,
        'distance',length(replace(((p_phash_v1 # x.phash_v1)::bit(64))::text,'0','')))), '[]'::jsonb) into conflicts
    from public.forward_schedule_reservation x
    where x.state='active'
      and (x.tenant_id is distinct from p_tenant_id or x.post_date is distinct from p_post_date
        or x.logical_post_id is distinct from p_logical_post_id)
      and (x.source_sha256=p_source_sha256
        or length(replace(((p_phash_v1 # x.phash_v1)::bit(64))::text,'0',''))<=30);
  return jsonb_build_object('allowed',jsonb_array_length(conflicts)=0,'conflicts',conflicts);
end;
$$;
revoke all on function public.check_reservation_conflicts_20261008(text,date,uuid,text,bigint)
  from public,anon,authenticated,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant execute on function public.check_reservation_conflicts_20261008(text,date,uuid,text,bigint) to service_role;

-- Consult-the-exact-reservation proof for the final publication path. Only an
-- ACTIVE reservation matching the row's current tenant, post_date, validated
-- logical post, source SHA and CURRENT revision resolves. Another date, gym,
-- logical post, source or a terminal reservation cannot be borrowed.
create function public.forward_reservation_proof_20261008(
  p_calendar_row_id uuid,p_source_sha256 text
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare
  r public.content_calendar%rowtype;
  tenant text;
  x public.forward_schedule_reservation%rowtype;
begin
  if p_calendar_row_id is null or p_source_sha256 is null or p_source_sha256 !~ '^[0-9a-f]{64}$' then
    raise exception 'exact reservation proof binding required' using errcode='22023';
  end if;
  select * into r from public.content_calendar where id=p_calendar_row_id;
  if not found or r.logical_post_id is null or r.post_date is null or nullif(btrim(r.gym_id),'') is null then
    raise exception 'schedule reservation proof unavailable' using errcode='23514';
  end if;
  select a.tenant_id into tenant from public.fixer_forward_media_tenant_alias_20261006 a
    where a.alias_key=btrim(r.gym_id);
  tenant:=coalesce(tenant,btrim(r.gym_id));
  select * into x from public.forward_schedule_reservation s
    where s.state='active' and s.tenant_id=tenant and s.post_date=r.post_date
      and s.logical_post_id=r.logical_post_id and s.source_sha256=p_source_sha256;
  if not found then
    raise exception 'schedule reservation proof unavailable' using errcode='23514';
  end if;
  return jsonb_build_object('reservation_id',x.reservation_id,'tenant_id',x.tenant_id,
    'post_date',x.post_date,'logical_post_id',x.logical_post_id,'source_sha256',x.source_sha256,
    'row_revision',x.row_revision,'attestation_ids',x.attestation_ids);
end;
$$;
revoke all on function public.forward_reservation_proof_20261008(uuid,text)
  from public,anon,authenticated,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant execute on function public.forward_reservation_proof_20261008(uuid,text) to service_role;
commit;
