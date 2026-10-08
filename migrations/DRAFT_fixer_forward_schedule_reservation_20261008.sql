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

-- Freeze logical identity when a NEW visual proof commits. Existing proofs get
-- explicit UNKNOWN evidence; never reconstruct historical authorization from
-- mutable calendar rows (even if they still exist).
create table public.forward_visual_claim_identity_20261008 (
 claim_token uuid primary key references public.forward_media_visual_claim_proof_20261008(claim_token),
 logical_post_id uuid,
 identity_state text not null check(identity_state in ('known','historical_unknown')),
 check((identity_state='known')=(logical_post_id is not null))
);
insert into public.forward_visual_claim_identity_20261008
 select claim_token,null,'historical_unknown' from public.forward_media_visual_claim_proof_20261008;
alter table public.forward_visual_claim_identity_20261008 enable row level security;
revoke all on public.forward_visual_claim_identity_20261008
 from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
create trigger immutable_row before update or delete on public.forward_visual_claim_identity_20261008
 for each row execute function public.fixer_forward_media_immutable_20261006();
create trigger immutable_truncate before truncate on public.forward_visual_claim_identity_20261008
 for each statement execute function public.fixer_forward_media_immutable_20261006();
create function public.freeze_forward_visual_identity_20261008()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
declare logical uuid;
begin
 select logical_post_id into logical from public.content_calendar where id=new.calendar_row_id for update;
 insert into public.forward_visual_claim_identity_20261008 values(new.claim_token,logical,case when logical is null then 'historical_unknown' else 'known' end);
 return new;
end;
$$;
revoke all on function public.freeze_forward_visual_identity_20261008()
 from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
create trigger freeze_logical_identity after insert on public.forward_media_visual_claim_proof_20261008
 for each row execute function public.freeze_forward_visual_identity_20261008();

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

-- Every sibling has its own frozen CURRENT proof; source equality alone cannot
-- lend another row's revision or attestation proof to publication.
create table public.forward_schedule_reservation_binding_20261008 (
 reservation_id uuid not null references public.forward_schedule_reservation(reservation_id),
 calendar_row_id uuid not null,
 row_revision text not null,
 lineage_evidence_id uuid not null,
 attestation_ids uuid[] not null check(cardinality(attestation_ids)=3),
 primary key(reservation_id,calendar_row_id,row_revision)
);
alter table public.forward_schedule_reservation_binding_20261008 enable row level security;
revoke all on public.forward_schedule_reservation_binding_20261008
 from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
create trigger immutable_row before update or delete on public.forward_schedule_reservation_binding_20261008
 for each row execute function public.fixer_forward_media_immutable_20261006();
create trigger immutable_truncate before truncate on public.forward_schedule_reservation_binding_20261008
 for each statement execute function public.fixer_forward_media_immutable_20261006();

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
  -- frozen claim identity must prove logical_post_id; historical unknown
  -- evidence fails closed even if a mutable calendar row appears to match). Any other tenant, date or logical post blocks. pHash <=6
  -- blocks cross logical post, 7-30 holds for review, >30 no match.
  for m in select rec.tenant_id, rec.post_date, ident.logical_post_id
      from public.forward_media_visual_attestation b
      join public.forward_media_visual_claim_proof_20261008 proof on b.attestation_id=any(proof.attestation_ids)
      join public.fixer_forward_media_claim_receipt_20261006 rec on rec.claim_token=proof.claim_token
      left join public.forward_visual_claim_identity_20261008 ident on ident.claim_token=proof.claim_token
      where b.source_sha256=src_sha loop
    if m.tenant_id is distinct from tenant or m.post_date is distinct from r.post_date
        or m.logical_post_id is distinct from p_logical_post_id then
      raise exception 'visual byte ancestry already consumed by another tenant/date/logical post' using errcode='23514';
    end if;
  end loop;
  for m in select b.phash_v1, rec.tenant_id, rec.post_date, ident.logical_post_id
      from public.forward_media_visual_attestation b
      join public.forward_media_visual_claim_proof_20261008 proof on b.attestation_id=any(proof.attestation_ids)
      join public.fixer_forward_media_claim_receipt_20261006 rec on rec.claim_token=proof.claim_token
      left join public.forward_visual_claim_identity_20261008 ident on ident.claim_token=proof.claim_token
      where b.phash_version=1 and b.source_sha256 is distinct from src_sha loop
    dist:=length(replace(((src_phash # m.phash_v1)::bit(64))::text,'0',''));
    if dist>30 then continue; end if;
    if m.tenant_id is not distinct from tenant and m.post_date is not distinct from r.post_date
        and m.logical_post_id is not distinct from p_logical_post_id then continue; end if;
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
      if exists(select 1 from public.forward_schedule_reservation_binding_20261008 b
       where b.reservation_id=slot.reservation_id and b.calendar_row_id=r.id and b.row_revision=base_revision
        and (b.lineage_evidence_id is distinct from evidence
         or not (b.attestation_ids @> p_attestation_ids and b.attestation_ids <@ p_attestation_ids))) then
       raise exception 'same revision reservation proof changed' using errcode='23514'; end if;
      insert into public.forward_schedule_reservation_binding_20261008
       values(slot.reservation_id,r.id,base_revision,evidence,p_attestation_ids) on conflict do nothing;
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
  insert into public.forward_schedule_reservation_binding_20261008
   values(ident,r.id,base_revision,evidence,p_attestation_ids);
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
  perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
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
  perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
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
      left join public.forward_visual_claim_identity_20261008 ident on ident.claim_token=proof.claim_token
    where (rec.tenant_id is distinct from p_tenant_id or rec.post_date is distinct from p_post_date
        or ident.logical_post_id is distinct from p_logical_post_id)
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
  binding public.forward_schedule_reservation_binding_20261008%rowtype;
  revision text;
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
  revision:=public.fixer_forward_media_attestation_request_20261006(r.id)->>'revision';
  select * into binding from public.forward_schedule_reservation_binding_20261008 b
   where b.reservation_id=x.reservation_id and b.calendar_row_id=r.id and b.row_revision=revision;
  if not found then raise exception 'current sibling reservation proof unavailable' using errcode='23514'; end if;
  return jsonb_build_object('reservation_id',x.reservation_id,'tenant_id',x.tenant_id,
    'post_date',x.post_date,'logical_post_id',x.logical_post_id,'source_sha256',x.source_sha256,
    'row_revision',binding.row_revision,'lineage_evidence_id',binding.lineage_evidence_id,'attestation_ids',binding.attestation_ids);
end;
$$;
revoke all on function public.forward_reservation_proof_20261008(uuid,text)
  from public,anon,authenticated,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant execute on function public.forward_reservation_proof_20261008(uuid,text) to service_role;

-- OFF preserves the preceding visual RPC contract. ON upgrades its final SQL
-- authority; renamed implementation cannot be invoked by service callers.
alter function public.fixer_forward_visual_index_claim_20261008(uuid,uuid,uuid,text,uuid[])
 rename to fixer_forward_visual_index_claim_internal_schedule_20261008;
revoke all on function public.fixer_forward_visual_index_claim_internal_schedule_20261008(uuid,uuid,uuid,text,uuid[])
 from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
create function public.fixer_forward_visual_index_claim_20261008(
 p_calendar_row_id uuid,p_claim_token uuid,p_evidence_id uuid,p_expected_revision text,p_attestation_ids uuid[]
) returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; tenant text; sha text; proof jsonb; m record; att record; distance integer;
begin
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'forward media authority requires read committed isolation' using errcode='25000'; end if;
 perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
 if not exists(select 1 from public.forward_schedule_reservation_gate_20261008 where singleton and enabled) then
  return public.fixer_forward_visual_index_claim_internal_schedule_20261008(
   p_calendar_row_id,p_claim_token,p_evidence_id,p_expected_revision,p_attestation_ids);
 end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 select * into r from public.content_calendar where id=p_calendar_row_id for update;
 if not found or r.logical_post_id is null or r.publish_claim_token is distinct from p_claim_token then
  raise exception 'exact reserved publication row required' using errcode='23514'; end if;
 select a.tenant_id into tenant from public.fixer_forward_media_tenant_alias_20261006 a where a.alias_key=btrim(r.gym_id);
 tenant:=coalesce(tenant,btrim(r.gym_id));
 select source_sha256 into sha from public.forward_media_visual_attestation
  where attestation_id=any(p_attestation_ids) and role='original';
 if sha is null then raise exception 'reservation original attestation unavailable' using errcode='23514'; end if;
 proof:=public.forward_reservation_proof_20261008(r.id,sha);
 if proof->>'row_revision' is distinct from p_expected_revision
  or (proof->>'lineage_evidence_id')::uuid is distinct from p_evidence_id
  or not ((array(select jsonb_array_elements_text(proof->'attestation_ids'))::uuid[]) @> p_attestation_ids
   and (array(select jsonb_array_elements_text(proof->'attestation_ids'))::uuid[]) <@ p_attestation_ids) then
  raise exception 'current sibling reservation proof differs from publication' using errcode='23514'; end if;
 -- Verify immutable identity for ALL outgoing roles. Original claim stack
 -- still validates trusted receipts, negative/history evidence and approvals.
 for att in select * from public.forward_media_visual_attestation where attestation_id=any(p_attestation_ids) loop
  for m in select b.source_sha256,b.phash_v1,rec.tenant_id,rec.post_date,i.logical_post_id
   from public.forward_media_visual_attestation b
   join public.forward_media_visual_claim_proof_20261008 p on b.attestation_id=any(p.attestation_ids)
   join public.fixer_forward_media_claim_receipt_20261006 rec on rec.claim_token=p.claim_token
   left join public.forward_visual_claim_identity_20261008 i on i.claim_token=p.claim_token loop
   distance:=length(replace(((att.phash_v1 # m.phash_v1)::bit(64))::text,'0',''));
   if btrim(m.tenant_id)=tenant and m.post_date=r.post_date and m.logical_post_id=r.logical_post_id then continue; end if;
   if m.source_sha256=att.source_sha256 or distance<=6 then
    raise exception 'visual byte ancestry already consumed by another tenant/date/logical post' using errcode='23514'; end if;
   if distance<=30 then raise exception 'visual similarity held for review' using errcode='23514'; end if;
  end loop;
 end loop;
 return public.fixer_forward_visual_index_claim_internal_schedule_20261008(
  p_calendar_row_id,p_claim_token,p_evidence_id,p_expected_revision,p_attestation_ids);
end;
$$;
revoke all on function public.fixer_forward_visual_index_claim_20261008(uuid,uuid,uuid,text,uuid[])
 from public,anon,authenticated,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant execute on function public.fixer_forward_visual_index_claim_20261008(uuid,uuid,uuid,text,uuid[]) to service_role;

-- Prevent fallback through the base public wrapper when schedule is ON but
-- the separate visual gate is OFF. Private base implementation remains private.
create or replace function public.fixer_claim_forward_media_20261006(
 p_calendar_row_id uuid,p_claim_token uuid,p_evidence_id uuid,p_expected_revision text
) returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
 if exists(select 1 from public.forward_schedule_reservation_gate_20261008 where singleton and enabled)
  or exists(select 1 from public.forward_media_visual_gate_20261008 where singleton and enabled) then
  raise exception 'visual index armed; protected claim proof required' using errcode='55000'; end if;
 return public.fixer_claim_forward_media_internal_20261008(p_calendar_row_id,p_claim_token,p_evidence_id,p_expected_revision);
end;
$$;

-- Add original SHA to the advisory visual proof response. Final SQL still
-- derives it independently from the trusted ids; response is not authority.
do $$
declare definition text;
begin
 definition:=pg_get_functiondef('public.fixer_forward_visual_proof_20261008(uuid,uuid,uuid,text)'::regprocedure);
 if strpos(definition,'return jsonb_build_object(''attestation_ids'',ids);')=0 then
  raise exception 'visual proof contract changed; review required' using errcode='23514'; end if;
 definition:=replace(definition,'return jsonb_build_object(''attestation_ids'',ids);',
  'return jsonb_build_object(''attestation_ids'',ids,''source_sha256'',(select source_sha256 from public.forward_media_visual_attestation where attestation_id=any(ids) and role=''original''));');
 execute definition;
end;
$$;

-- One atomic switch. Staging is persisted before attestation, inactive and
-- unapproved. No network reads here. Exact old snapshots prevent stale writes;
-- any failing candidate aborts activation, all reservations and supersession.
create function public.finalize_forward_schedule_batch_20261008(
 p_tenant_id text,p_candidates jsonb,p_expected_old_rows jsonb
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare item jsonb; old_snapshot jsonb; r public.content_calendar%rowtype; tenant text;
 candidates uuid[]; old_ids uuid[]; row_ids uuid[]:=array[]::uuid[]; reservations uuid[]:=array[]::uuid[];
 reservation uuid; ids uuid[]; expected uuid;
begin
 if nullif(btrim(p_tenant_id),'') is null or jsonb_typeof(p_candidates) is distinct from 'array'
  or jsonb_array_length(p_candidates)=0 or jsonb_typeof(p_expected_old_rows) is distinct from 'array' then
  raise exception 'exact schedule batch binding required' using errcode='22023'; end if;
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'forward media authority requires read committed isolation' using errcode='25000'; end if;
 perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
 if not exists(select 1 from public.forward_schedule_reservation_gate_20261008 where singleton and enabled) then
  raise exception 'schedule reservation authority is OFF pending review' using errcode='55000'; end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 select array_agg((v->>'calendar_row_id')::uuid) into candidates from jsonb_array_elements(p_candidates) v;
 select coalesce(array_agg((v->>'id')::uuid),array[]::uuid[]) into old_ids from jsonb_array_elements(p_expected_old_rows) v;
 if exists(select 1 from unnest(candidates||old_ids) id group by id having count(*)>1) or array_position(candidates,null) is not null
  or array_position(old_ids,null) is not null then
  raise exception 'duplicate or missing calendar batch identity' using errcode='22023'; end if;
 -- Deterministic row order; census serializes all cooperating transactions.
 perform 1 from public.content_calendar where id=any(candidates||old_ids) order by id for update;
 for old_snapshot in select value from jsonb_array_elements(p_expected_old_rows) loop
  select * into r from public.content_calendar where id=(old_snapshot->>'id')::uuid;
  if not found or to_jsonb(r) is distinct from old_snapshot or r.status is null or r.status not in ('pending','draft')
   or r.variant_status is distinct from 'active' or r.media_not_ready_reason is not null or r.publish_claim_token is not null
   or r.publish_reservation_day is not null or r.published_at is not null or r.late_post_id is not null then
   raise exception 'old calendar snapshot changed or protected' using errcode='23514'; end if;
  select a.tenant_id into tenant from public.fixer_forward_media_tenant_alias_20261006 a where a.alias_key=btrim(r.gym_id);
  tenant:=coalesce(tenant,btrim(r.gym_id));
  if tenant is distinct from btrim(p_tenant_id) then raise exception 'batch tenant mismatch' using errcode='23514'; end if;
 end loop;
 for item in select value from jsonb_array_elements(p_candidates) loop
  select * into r from public.content_calendar where id=(item->>'calendar_row_id')::uuid;
  if not found or r.variant_status is distinct from 'candidate' or r.media_not_ready_reason is distinct from 'forward_reservation_staged' or r.status is null or r.status not in ('pending','draft')
   or r.publish_claim_token is not null or r.published_at is not null or r.late_post_id is not null then
   raise exception 'inactive unapproved schedule candidate required' using errcode='23514'; end if;
  select a.tenant_id into tenant from public.fixer_forward_media_tenant_alias_20261006 a where a.alias_key=btrim(r.gym_id);
  tenant:=coalesce(tenant,btrim(r.gym_id));
  if tenant is distinct from btrim(p_tenant_id) then raise exception 'batch tenant mismatch' using errcode='23514'; end if;
  ids:=array(select jsonb_array_elements_text(item->'attestation_ids'))::uuid[];
  expected:=nullif(item->>'expected_reservation_id','')::uuid;
  update public.content_calendar set variant_status='active',media_not_ready_reason=null where id=r.id;
  reservation:=public.reserve_forward_slot_20261008(r.id,(item->>'logical_post_id')::uuid,item->>'expected_revision',ids,expected);
  row_ids:=array_append(row_ids,r.id);
  reservations:=array_append(reservations,reservation);
 end loop;
 -- No old rows change until ALL candidates have passed. Entire function is a
 -- transaction, so any following constraint/trigger failure also rolls back.
 update public.content_calendar set variant_status='archived' where id=any(old_ids);
 for reservation in select x.reservation_id from public.forward_schedule_reservation x
  where x.state='active' and x.calendar_row_id=any(old_ids) and not (x.reservation_id=any(reservations)) loop
  perform public.release_forward_slot_20261008(reservation,'atomic-calendar-replacement');
 end loop;
 return jsonb_build_object('row_ids',row_ids,'reservation_ids',reservations);
end;
$$;
revoke all on function public.finalize_forward_schedule_batch_20261008(text,jsonb,jsonb)
 from public,anon,authenticated,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant execute on function public.finalize_forward_schedule_batch_20261008(text,jsonb,jsonb) to service_role;

-- Read only gate predicate allows the invoker guard to preserve OFF behavior
-- for existing calendar roles without granting them gate-table DML or SELECT.
create function public.forward_schedule_enabled_20261008()
returns boolean language sql stable security definer set search_path=pg_catalog,public as $$
 select exists(select 1 from public.forward_schedule_reservation_gate_20261008 where singleton and enabled);
$$;
revoke all on function public.forward_schedule_enabled_20261008() from public;
grant execute on function public.forward_schedule_enabled_20261008()
 to anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;

-- Statement lock runs BEFORE row locks, maintaining graph -> census -> row.
-- Direct service writes cannot activate staged rows or supersede/delete active
-- logical calendar rows while authority is armed. Existing approval/publish
-- state transitions retain their separate guarded workflow.
create function public.forward_schedule_calendar_lock_20261008()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
 if exists(select 1 from public.forward_schedule_reservation_gate_20261008 where singleton and enabled) then
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0)); end if;
 return null;
end;
$$;
create function public.forward_schedule_calendar_guard_20261008()
returns trigger language plpgsql security invoker set search_path=pg_catalog,public as $$
begin
 if current_user::regrole::oid=(select proowner from pg_proc where oid='public.finalize_forward_schedule_batch_20261008(text,jsonb,jsonb)'::regprocedure)
  or not public.forward_schedule_enabled_20261008() then
  return coalesce(new,old); end if;
 if (tg_op='INSERT' and new.logical_post_id is not null and new.variant_status='active')
  or (tg_op='UPDATE' and new.variant_status='active' and old.variant_status is distinct from 'active')
  or (tg_op='UPDATE' and old.logical_post_id is not null and old.variant_status='active' and new.variant_status is distinct from 'active')
  or (tg_op='DELETE' and old.logical_post_id is not null and old.variant_status='active') then
  raise exception 'logical schedule activation and replacement are atomic RPC-managed only' using errcode='42501'; end if;
 return coalesce(new,old);
end;
$$;
-- Service can read the protected gate for planner readback, not mutate it.
-- Other existing roles use only the definer boolean predicate in the guard.
grant select on public.forward_schedule_reservation_gate_20261008 to service_role;
create policy service_gate_read on public.forward_schedule_reservation_gate_20261008 for select to service_role using(true);
revoke all on function public.forward_schedule_calendar_lock_20261008(),public.forward_schedule_calendar_guard_20261008()
 from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
create trigger forward_schedule_calendar_lock before insert or update or delete on public.content_calendar
 for each statement execute function public.forward_schedule_calendar_lock_20261008();
create trigger forward_schedule_calendar_guard before insert or update or delete on public.content_calendar
 for each row execute function public.forward_schedule_calendar_guard_20261008();
commit;
