-- DRAFT / UNAPPLIED / DEFAULT OFF. No production activation is authorized.
-- PROSPECTIVE PHOTO OCCUPANCY: one independently evidenced approved still
-- photo may obtain PERMANENT GLOBAL prospective occupancy BEFORE any calendar
-- exposure, with its exact source and rendered derivative byte identities
-- bound to exactly one (tenant, post_date, logical_post_id). A second
-- tenant/date/logical post or any known derivative ancestry must fail.
--
-- This draft is ADDITIVE and composes LAST, on top of:
--   DRAFT_fixer_forward_media_claim_20261006.sql        (base claim stack),
--   DRAFT_fixer_forward_visual_index_20261008.sql       (visual attestation/
--                                                        negative/proof),
--   DRAFT_fixer_forward_media_source_history_20261007.sql (durable source
--                                                        receipts, historical
--                                                        originals),
--   DRAFT_fixer_forward_media_photo_certificate_20261007.sql (independently
--                                                        signed eligibility),
--   DRAFT_fixer_forward_schedule_reservation_20261008.sql (releasable
--                                                        reservations),
--   DRAFT_fixer_photo_historical_clearance_20261008.sql (no-exclusions guard).
-- Apply order: the full stack above, then this draft, then REAPPLY
-- DRAFT_fixer_photo_historical_clearance_20261008.sql (idempotent, retains
-- OIDs/ACLs) so its "apply last" invariant still holds. This draft calls the
-- guard explicitly in its own admission body instead of relying on the
-- guard's body rewrite (whose fixed name list does not include this RPC).
--
-- What this layer is NOT: it never replaces, bypasses or re-implements the
-- publish claim (fixer_forward_media_use_20261006 / claim receipts remain the
-- sole publication occupancy) or the releasable schedule reservation lane.
-- Prospective occupancy only CONSULTS those ledgers and is itself consulted
-- through the explicit proof RPC below. A prospective receipt is never a
-- ready-row bypass, a claim bypass, a reservation bypass, or historical
-- clearance evidence: the historical lane retains its no-exclusions guard,
-- and final publication must still pass the full visual claim stack.
--
-- Lock order matches the existing stack: graph -> census -> row -> slot.
-- No network/object I/O; no locks held across fetches. Unknown or ambiguous
-- historical evidence HOLDS (fail closed); newly ingested media is never
-- treated as historically unused — admission REQUIRES the independently
-- signed photo certificate over the complete current corpus with zero
-- authoritative baseline exclusions. Ledger rows are durable: no delete;
-- state transitions one-way active -> revoked; revoked rows persist as
-- occupancy evidence and terminally fence their (tenant, source) pair.
-- Rollback before use: remove the new objects. After use preserve all rows.
begin;

-- Hard prerequisites: fail the whole migration if any composed layer or the
-- no-exclusions guard is absent.
do $$
begin
  if to_regprocedure('public.fixer_claim_forward_media_internal_20261008(uuid,uuid,uuid,text)') is null
      or to_regclass('public.fixer_forward_media_use_20261006') is null
      or to_regclass('public.fixer_forward_media_claim_receipt_20261006') is null then
    raise exception 'forward media claim draft is required' using errcode='23514';
  end if;
  if to_regclass('public.forward_media_visual_attestation') is null
      or to_regclass('public.forward_media_visual_negative') is null
      or to_regclass('public.forward_media_visual_claim_proof_20261008') is null
      or to_regclass('public.forward_visual_claim_identity_20261008') is null then
    raise exception 'forward visual index and reservation identity drafts are required' using errcode='23514';
  end if;
  if to_regclass('public.fixer_forward_media_source_receipt_20261007') is null
      or to_regclass('public.fixer_forward_media_historical_original_20261007') is null then
    raise exception 'forward media source history draft is required' using errcode='23514';
  end if;
  if to_regprocedure('public.fixer_forward_media_photo_record_20261007(text,text,text)') is null then
    raise exception 'photo certificate draft is required' using errcode='23514';
  end if;
  if to_regprocedure('public.reserve_forward_slot_20261008(uuid,uuid,text,uuid[],uuid)') is null
      or to_regclass('public.forward_schedule_reservation') is null then
    raise exception 'forward schedule reservation draft is required' using errcode='23514';
  end if;
  if to_regprocedure('public.fixer_assert_photo_no_exclusions_20261008()') is null then
    raise exception 'no-exclusions historical guard is required; apply DRAFT_fixer_photo_historical_clearance_20261008 first' using errcode='23514';
  end if;
  if not exists(select 1 from pg_catalog.pg_attribute
    where attrelid='public.content_calendar'::regclass and attname='logical_post_id' and not attisdropped) then
    raise exception 'content_calendar.logical_post_id is required' using errcode='23514';
  end if;
end;
$$;

-- Protected singleton activation gate, default OFF. Gate writes serialize
-- with admissions through the exclusive graph lock (shared gate-lock trigger
-- helper from the visual index stack).
create table public.forward_prospective_photo_gate_20261008 (
  singleton boolean primary key default true check(singleton),
  enabled boolean not null default false
);
insert into public.forward_prospective_photo_gate_20261008 values(true,false);
alter table public.forward_prospective_photo_gate_20261008 enable row level security;
revoke all on public.forward_prospective_photo_gate_20261008
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
create trigger prospective_gate_lock before insert or update or delete on public.forward_prospective_photo_gate_20261008
  for each row execute function public.fixer_forward_visual_gate_lock_20261008();
-- Service can read the protected gate for planner readback, not mutate it.
grant select on public.forward_prospective_photo_gate_20261008 to service_role;
create policy service_gate_read on public.forward_prospective_photo_gate_20261008
  for select to service_role using(true);

-- Permanent global prospective occupancy ledger. At most one ACTIVE occupancy
-- per exact source SHA (global) and per (tenant, post_date, logical_post_id)
-- slot. Source AND rendered derivative byte identities (trusted attester
-- object-read receipts: exact URL, md5, byte length) are frozen on the row.
-- Terminal rows persist as occupancy evidence; there is no delete path.
create table public.forward_prospective_photo_occupancy_20261008 (
  occupancy_id uuid primary key default gen_random_uuid(),
  tenant_id text not null check (tenant_id=btrim(tenant_id) and tenant_id<>''),
  calendar_row_id uuid not null,
  logical_post_id uuid not null,
  post_date date not null,
  source_asset_id text not null check (source_asset_id=btrim(source_asset_id) and source_asset_id<>''),
  source_url text not null check (source_url ~ '^https://[^[:space:]]+$'),
  source_sha256 text not null check (source_sha256 ~ '^[0-9a-f]{64}$'),
  phash_version integer not null default 1 check (phash_version=1),
  phash_v1 bigint not null,
  source_read_receipt uuid not null references public.fixer_forward_media_object_read_20261006(receipt_id),
  image_read_receipt uuid not null references public.fixer_forward_media_object_read_20261006(receipt_id),
  thumbnail_read_receipt uuid references public.fixer_forward_media_object_read_20261006(receipt_id),
  source_md5 text not null check (source_md5 ~ '^[0-9a-f]{32}$'),
  source_length bigint not null check (source_length>0 and source_length<=134217728),
  image_md5 text not null check (image_md5 ~ '^[0-9a-f]{32}$'),
  image_length bigint not null check (image_length>0 and image_length<=134217728),
  thumbnail_md5 text check (thumbnail_md5 is null or thumbnail_md5 ~ '^[0-9a-f]{32}$'),
  thumbnail_length bigint check (thumbnail_length is null or (thumbnail_length>0 and thumbnail_length<=134217728)),
  check ((thumbnail_md5 is null) = (thumbnail_length is null)),
  check ((thumbnail_read_receipt is null) = (thumbnail_md5 is null)),
  row_revision text not null check (btrim(row_revision)<>''),
  lineage_evidence_id uuid not null references public.fixer_forward_media_lineage_20261006(evidence_id),
  attestation_ids uuid[] not null check(cardinality(attestation_ids)=3),
  audit_id uuid not null references public.fixer_forward_media_photo_certificate_20261007(audit_id),
  state text not null default 'active' check (state in ('active','revoked')),
  state_reason text check (state_reason is null or btrim(state_reason)<>''),
  created_at timestamptz not null default now(),
  state_changed_at timestamptz
);
create unique index forward_prospective_photo_active_sha_20261008
  on public.forward_prospective_photo_occupancy_20261008(source_sha256) where state='active';
create unique index forward_prospective_photo_active_slot_20261008
  on public.forward_prospective_photo_occupancy_20261008(tenant_id,post_date,logical_post_id) where state='active';
create index forward_prospective_photo_phash_20261008
  on public.forward_prospective_photo_occupancy_20261008(phash_v1) where state='active';
create index forward_prospective_photo_receipts_20261008
  on public.forward_prospective_photo_occupancy_20261008(source_read_receipt,image_read_receipt,thumbnail_read_receipt);

-- Write guard: only the SECURITY DEFINER admission/revocation RPCs (whose
-- owner is the migration owner) may write; transitions are one-way and proof
-- columns are immutable once written. SECURITY INVOKER keeps the effective
-- owner inside the definer RPCs but sees the real role on direct DML.
create function public.fixer_forward_prospective_guard_20261008()
returns trigger language plpgsql security invoker set search_path=pg_catalog,public as $$
begin
  if current_user::regrole::oid is distinct from (
    select p.proowner from pg_catalog.pg_proc p
    where p.oid='public.admit_prospective_photo_occupancy_20261008(uuid,uuid,text,uuid[],uuid)'::regprocedure
  ) then
    raise exception 'prospective photo occupancy is RPC-managed only' using errcode='42501';
  end if;
  if tg_op='DELETE' then
    raise exception 'prospective photo occupancy is durable; deletion forbidden' using errcode='23514';
  end if;
  if tg_op='INSERT' and new.state is distinct from 'active' then
    raise exception 'prospective photo occupancy is inserted active only' using errcode='23514';
  end if;
  if tg_op='UPDATE' then
    if old.state is distinct from 'active' or new.state is distinct from 'revoked'
        or new.occupancy_id is distinct from old.occupancy_id
        or new.tenant_id is distinct from old.tenant_id
        or new.calendar_row_id is distinct from old.calendar_row_id
        or new.logical_post_id is distinct from old.logical_post_id
        or new.post_date is distinct from old.post_date
        or new.source_asset_id is distinct from old.source_asset_id
        or new.source_url is distinct from old.source_url
        or new.source_sha256 is distinct from old.source_sha256
        or new.phash_version is distinct from old.phash_version
        or new.phash_v1 is distinct from old.phash_v1
        or new.source_read_receipt is distinct from old.source_read_receipt
        or new.image_read_receipt is distinct from old.image_read_receipt
        or new.thumbnail_read_receipt is distinct from old.thumbnail_read_receipt
        or new.source_md5 is distinct from old.source_md5
        or new.source_length is distinct from old.source_length
        or new.image_md5 is distinct from old.image_md5
        or new.image_length is distinct from old.image_length
        or new.thumbnail_md5 is distinct from old.thumbnail_md5
        or new.thumbnail_length is distinct from old.thumbnail_length
        or new.row_revision is distinct from old.row_revision
        or new.lineage_evidence_id is distinct from old.lineage_evidence_id
        or new.attestation_ids is distinct from old.attestation_ids
        or new.audit_id is distinct from old.audit_id
        or new.created_at is distinct from old.created_at then
      raise exception 'prospective photo occupancy proof is immutable; only one-way revocation allowed' using errcode='23514';
    end if;
  end if;
  return coalesce(new,old);
end;
$$;
revoke all on function public.fixer_forward_prospective_guard_20261008()
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
create trigger prospective_guard before insert or update or delete on public.forward_prospective_photo_occupancy_20261008
  for each row execute function public.fixer_forward_prospective_guard_20261008();
create trigger prospective_guard_truncate before truncate on public.forward_prospective_photo_occupancy_20261008
  for each statement execute function public.fixer_forward_media_immutable_20261006();
alter table public.forward_prospective_photo_occupancy_20261008 enable row level security;
revoke all on public.forward_prospective_photo_occupancy_20261008
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant select on public.forward_prospective_photo_occupancy_20261008 to service_role;
create policy service_read on public.forward_prospective_photo_occupancy_20261008
  for select to service_role using(true);

-- Every admitted sibling row has its own frozen CURRENT proof; source equality
-- alone cannot lend another row's revision or attestation proof to occupancy.
create table public.forward_prospective_photo_binding_20261008 (
  occupancy_id uuid not null references public.forward_prospective_photo_occupancy_20261008(occupancy_id),
  calendar_row_id uuid not null,
  row_revision text not null,
  lineage_evidence_id uuid not null,
  attestation_ids uuid[] not null check(cardinality(attestation_ids)=3),
  primary key(occupancy_id,calendar_row_id,row_revision)
);
alter table public.forward_prospective_photo_binding_20261008 enable row level security;
revoke all on public.forward_prospective_photo_binding_20261008
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
create trigger immutable_row before update or delete on public.forward_prospective_photo_binding_20261008
  for each row execute function public.fixer_forward_media_immutable_20261006();
create trigger immutable_truncate before truncate on public.forward_prospective_photo_binding_20261008
  for each statement execute function public.fixer_forward_media_immutable_20261006();

-- OWNER-ONLY atomic prospective admission. Validates tenant, local date,
-- logical post, CURRENT source/clearance/revision, the independently signed
-- photo certificate for THIS exact candidate, and trusted byte receipts for
-- the source and every rendered derivative; consults negative evidence,
-- committed permanent use (claims/use ledger), historical originals, active
-- reservations and existing prospective occupancy; inserts one durable
-- occupancy in this ONE transaction. The no-exclusions historical guard is
-- invoked EXPLICITLY (entry and after the census lock): this layer never
-- silently bypasses it. Callable only by the forward media owner role;
-- service_role, attester, anon and authenticated have no path.
create function public.admit_prospective_photo_occupancy_20261008(
  p_calendar_row_id uuid,p_logical_post_id uuid,p_expected_revision text,
  p_attestation_ids uuid[],p_audit_id uuid
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
  src_receipt uuid;
  img_receipt uuid;
  thumb_receipt uuid;
  src_md5 text;
  src_len bigint;
  img_md5 text;
  img_len bigint;
  thumb_md5 text;
  thumb_len bigint;
  cert public.fixer_forward_media_photo_certificate_20261007%rowtype;
  candidate jsonb;
  approved jsonb;
  slot public.forward_prospective_photo_occupancy_20261008%rowtype;
  ident uuid;
begin
  if p_calendar_row_id is null or p_logical_post_id is null
      or nullif(btrim(p_expected_revision),'') is null or p_attestation_ids is null
      or cardinality(p_attestation_ids)<>3 or p_audit_id is null then
    raise exception 'exact prospective occupancy binding required' using errcode='22023';
  end if;
  if current_setting('transaction_isolation')<>'read committed' then
    raise exception 'forward media authority requires read committed isolation' using errcode='25000';
  end if;
  -- The prospective lane is explicitly guarded at entry: unknown/ambiguous
  -- historical evidence holds before any authority is considered.
  perform public.fixer_assert_photo_no_exclusions_20261008();
  -- Lock order: graph -> census -> row -> slot (matches the claim stack).
  perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
  if not exists(select 1 from public.forward_prospective_photo_gate_20261008 where singleton and enabled) then
    raise exception 'prospective photo authority is OFF pending review' using errcode='55000';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
  -- Recheck after the census lock to cover a baseline changed while waiting.
  perform public.fixer_assert_photo_no_exclusions_20261008();
  -- Prospective admission precedes calendar exposure: only unsent, unclaimed,
  -- planned rows may bind (identical fence to the reservation lane).
  select * into r from public.content_calendar where id=p_calendar_row_id for update;
  if not found or r.status is null or r.status not in ('draft','pending','queued','approved')
      or r.publish_claim_token is not null
      or r.published_at is not null or r.late_post_id is not null
      or r.variant_status is distinct from 'active'
      or r.media_not_ready_reason is not null
      or r.post_date is null
      or nullif(btrim(r.gym_id),'') is null then
    raise exception 'prospective occupancy requires an unsent planned calendar row' using errcode='23514';
  end if;
  if r.logical_post_id is null or r.logical_post_id is distinct from p_logical_post_id then
    raise exception 'logical post binding invalid' using errcode='23514';
  end if;
  select a.tenant_id into tenant from public.fixer_forward_media_tenant_alias_20261006 a
    where a.alias_key=btrim(r.gym_id);
  tenant:=coalesce(tenant,nullif(btrim(r.gym_id),''));
  if tenant is null then raise exception 'canonical tenant mapping unavailable' using errcode='23514'; end if;
  -- Bind the exact persisted outgoing media revision. Changed media
  -- invalidates the admission request; approval is never inherited after
  -- changed media.
  base_revision:=public.fixer_forward_media_attestation_request_20261006(r.id)->>'revision';
  expected_revision:=('x'||substr(base_revision,1,15))::bit(60)::bigint;
  if base_revision is distinct from p_expected_revision then
    raise exception 'outgoing media revision changed' using errcode='23514';
  end if;
  -- Serialize same-slot admissions AFTER the row lock to keep the global order.
  perform pg_advisory_xact_lock(hashtextextended(
    jsonb_build_array('fixer_forward_slot_20261008',tenant,r.post_date,p_logical_post_id)::text,0));
  -- CURRENT source authority: owner registry tuple must match the row, and the
  -- exact tuple must hold cleared_unused with no fleet hold for its
  -- fingerprint. Unknown history holds; an empty index grants no clearance.
  -- Newly ingested media is never treated as historically unused here.
  perform public.fixer_forward_media_provenance_lookup_20261006(r.id);
  -- Revocation fence: a revoked occupancy makes its (tenant, source asset)
  -- terminal under this draft. Reinstatement requires a fresh source identity
  -- with fresh owner clearance and a fresh independent certificate, never an
  -- occupancy overwrite.
  if exists(select 1 from public.forward_prospective_photo_occupancy_20261008 x
      where x.tenant_id=tenant and x.source_asset_id=r.source_media_asset_id and x.state='revoked') then
    raise exception 'prospective source revoked; fresh source identity, clearance and certificate required' using errcode='23514';
  end if;
  -- Independently attested eligibility: the exact signed photo certificate
  -- for THIS candidate. The signed candidate must match the locked row, the
  -- canonical tenant, the exact source identity and the exact rendered
  -- derivative URLs; the certificate key must remain approved and unrevoked.
  select * into cert from public.fixer_forward_media_photo_certificate_20261007 c
    where c.audit_id=p_audit_id;
  if not found then
    raise exception 'independent photo certificate unavailable' using errcode='23514';
  end if;
  candidate:=(cert.payload_json::jsonb)->'candidate';
  approved:=public.fixer_forward_media_photo_approved_key_20261007(cert.key_id);
  if coalesce((approved->>'approved')::boolean,false) is not true then
    raise exception 'approved independent signer required' using errcode='23514';
  end if;
  if cert.calendar_row_id is distinct from r.id
      or (cert.payload_json::jsonb)->>'decision' is distinct from 'reviewed_no_prior_visual_use'
      or candidate->>'calendar_row_id' is distinct from r.id::text
      or candidate->>'tenant_id' is distinct from tenant
      or candidate->>'tenant_id' is distinct from r.gym_id
      or candidate->>'group_key' is distinct from r.visual_group_key
      or candidate->>'post_date' is distinct from r.post_date::text
      or candidate->>'source_asset_id' is distinct from r.source_media_asset_id
      or candidate->>'source_url' is distinct from r.source_media_url
      or candidate->>'image_url' is distinct from r.image_url
      or candidate->>'thumbnail_url' is distinct from r.thumbnail_url
      or candidate->>'source_receipt_ref' is null then
    raise exception 'prospective certificate candidate mismatch' using errcode='23514';
  end if;
  -- The durable original source receipt behind the certificate must still
  -- resolve to the exact same bytes.
  if not exists(select 1 from public.fixer_forward_media_source_receipt_20261007 s
      where s.receipt_ref=candidate->>'source_receipt_ref'
        and s.calendar_row_id=r.id and s.tenant_id=tenant
        and s.source_asset_id=r.source_media_asset_id
        and s.exact_source_url=r.source_media_url
        and s.source_fingerprint=candidate->>'source_fingerprint'
        and s.source_sha256=candidate->>'source_sha256'
        and s.source_length=(candidate->>'source_length')::bigint) then
    raise exception 'exact durable original receipt required' using errcode='23514';
  end if;
  -- Verify every attestation against trusted persisted evidence: exactly the
  -- three roles, this tenant, the current revision, one shared lineage, exact
  -- URLs and trusted object-read receipts (md5 + length). Capture the frozen
  -- source AND rendition byte identities from the trusted receipts.
  for att in select a.* from public.forward_media_visual_attestation a
    where a.attestation_id=any(p_attestation_ids) loop
    if att.tenant_key is distinct from tenant
        or att.row_revision is distinct from expected_revision
        or att.role=any(seen_roles)
        or (evidence is not null and att.lineage_receipt_id is distinct from evidence) then
      raise exception 'prospective attestation evidence invalid' using errcode='23514';
    end if;
    evidence:=att.lineage_receipt_id;
    seen_roles:=array_append(seen_roles,att.role);
    expected_url:=case att.role
      when 'original' then r.source_media_url
      when 'delivered' then r.image_url
      else coalesce(r.thumbnail_url,r.image_url) end;
    if expected_url is null or att.media_url is distinct from expected_url then
      raise exception 'prospective attestation evidence invalid' using errcode='23514';
    end if;
    if not exists(select 1 from public.fixer_forward_media_object_read_20261006 o
        where o.receipt_id=att.object_read_receipt_id and o.tenant_id=tenant
          and o.exact_url=att.media_url and o.fingerprint='md5:'||att.source_md5
          and o.byte_length=att.byte_length
          and o.receipt_id=(select case att.role when 'original' then l.source_read_receipt
              when 'delivered' then l.image_read_receipt
              else coalesce(l.thumbnail_read_receipt,l.image_read_receipt) end
            from public.fixer_forward_media_lineage_20261006 l where l.evidence_id=evidence)) then
      raise exception 'prospective attestation evidence invalid' using errcode='23514';
    end if;
    if att.role='original' then
      src_sha:=att.source_sha256;
      src_phash:=att.phash_v1;
      src_receipt:=att.object_read_receipt_id;
      src_md5:=att.source_md5;
      src_len:=att.byte_length;
    elsif att.role='delivered' then
      img_receipt:=att.object_read_receipt_id;
      img_md5:=att.source_md5;
      img_len:=att.byte_length;
    else
      thumb_receipt:=att.object_read_receipt_id;
      thumb_md5:=att.source_md5;
      thumb_len:=att.byte_length;
    end if;
  end loop;
  if cardinality(seen_roles)<>3 or src_sha is null then
    raise exception 'prospective attestation evidence invalid' using errcode='23514';
  end if;
  -- The signed certificate candidate's byte identities must equal the trusted
  -- attester receipts; a certificate over other bytes grants no occupancy.
  if candidate->>'source_sha256' is distinct from 'sha256:'||src_sha
      or candidate->>'source_fingerprint' is distinct from 'md5:'||src_md5
      or (candidate->>'source_length')::bigint is distinct from src_len
      or candidate->>'image_fingerprint' is distinct from 'md5:'||img_md5
      or (candidate->>'image_length')::bigint is distinct from img_len then
    raise exception 'prospective certificate byte identity mismatch' using errcode='23514';
  end if;
  if not exists(select 1 from public.fixer_forward_media_lineage_20261006 l
      where l.evidence_id=evidence and l.calendar_row_id=r.id and l.row_revision=base_revision
        and l.tenant_id=tenant and l.group_key=r.visual_group_key
        and l.source_asset_id=r.source_media_asset_id and l.manifest_digest=r.render_manifest_digest) then
    raise exception 'prospective lineage binding invalid' using errcode='23514';
  end if;
  -- Late negative evidence blocks admission: exact SHA or pHash <= 30.
  if exists(select 1 from public.forward_media_visual_negative n
      where n.source_sha256=src_sha
        or (n.phash_version=1
          and length(replace(((src_phash # n.phash_v1)::bit(64))::text,'0',''))<=30)) then
    raise exception 'visual negative evidence blocks prospective occupancy' using errcode='23514';
  end if;
  -- Known derivative/byte ancestry in durable history: an audited historical
  -- original with the same source bytes blocks. Ambiguity already held above;
  -- a match here is definitive reuse.
  if exists(select 1 from public.fixer_forward_media_historical_original_20261007 h
      where h.source_fingerprint='md5:'||src_md5 or h.source_sha256='sha256:'||src_sha) then
    raise exception 'historical byte ancestry already consumed' using errcode='23514';
  end if;
  -- Permanent use ledger: the exact source or any exact rendered derivative
  -- fingerprint already claimed is consumed forever. Same-day sibling reuse
  -- cannot apply: this ledger records only committed publication occupancy.
  if exists(select 1 from public.fixer_forward_media_use_20261006 u
      where u.fingerprint=any(array['md5:'||src_md5,'md5:'||img_md5,'md5:'||coalesce(thumb_md5,'')])) then
    raise exception 'permanent use ledger already consumes these bytes' using errcode='23514';
  end if;
  -- Committed visual occupancy (claims): equal SHA is reusable ONLY by exact
  -- same tenant/date/logical-post siblings; the frozen claim identity must
  -- prove logical_post_id, historical unknown evidence fails closed. Any
  -- other tenant, date or logical post blocks. pHash <=6 blocks cross logical
  -- post, 7-30 holds for review, >30 no match.
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
  -- Active schedule reservations on other slots: equal SHA blocks; pHash
  -- policy identical to committed occupancy. Own slot is allowed to coexist:
  -- reservation and prospective occupancy are separate complementary lanes.
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
  -- Existing prospective occupancy on other slots: equal SHA blocks; pHash
  -- policy identical. Own slot resolves below.
  for m in select x.occupancy_id, x.phash_v1 from public.forward_prospective_photo_occupancy_20261008 x
      where x.state='active' and x.source_sha256=src_sha
        and (x.tenant_id is distinct from tenant or x.post_date is distinct from r.post_date
          or x.logical_post_id is distinct from p_logical_post_id) loop
    raise exception 'source already prospectively occupied for another tenant/date/logical post' using errcode='23514';
  end loop;
  for m in select x.occupancy_id, x.phash_v1 from public.forward_prospective_photo_occupancy_20261008 x
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
  -- Slot resolution under the slot advisory lock; the partial unique indexes
  -- are the final backstop so at most one active occupancy exists per slot
  -- and per exact source SHA.
  select * into slot from public.forward_prospective_photo_occupancy_20261008 x
    where x.state='active' and x.tenant_id=tenant and x.post_date=r.post_date
      and x.logical_post_id=p_logical_post_id;
  if found then
    -- Idempotent exact same-slot retry / same-day sibling reuse of its own
    -- visual: the caller's proof for THIS row was fully validated above, so
    -- an identical source on the same slot returns the existing occupancy
    -- (sibling rows carry their own per-row revisions and attestation ids).
    if slot.source_sha256=src_sha then
      if exists(select 1 from public.forward_prospective_photo_binding_20261008 b
       where b.occupancy_id=slot.occupancy_id and b.calendar_row_id=r.id and b.row_revision=base_revision
        and (b.lineage_evidence_id is distinct from evidence
         or not (b.attestation_ids @> p_attestation_ids and b.attestation_ids <@ p_attestation_ids))) then
       raise exception 'same revision prospective proof changed' using errcode='23514'; end if;
      insert into public.forward_prospective_photo_binding_20261008
       values(slot.occupancy_id,r.id,base_revision,evidence,p_attestation_ids) on conflict do nothing;
      return slot.occupancy_id;
    end if;
    -- Prospective occupancy is permanent: no CAS supersede. A different
    -- source on the same slot is a hard conflict.
    raise exception 'prospective occupancy slot conflict' using errcode='23514';
  end if;
  insert into public.forward_prospective_photo_occupancy_20261008
    (tenant_id,calendar_row_id,logical_post_id,post_date,source_asset_id,source_url,
     source_sha256,phash_v1,source_read_receipt,image_read_receipt,thumbnail_read_receipt,
     source_md5,source_length,image_md5,image_length,thumbnail_md5,thumbnail_length,
     row_revision,lineage_evidence_id,attestation_ids,audit_id)
    values(tenant,r.id,p_logical_post_id,r.post_date,r.source_media_asset_id,r.source_media_url,
      src_sha,src_phash,src_receipt,img_receipt,thumb_receipt,
      src_md5,src_len,img_md5,img_len,thumb_md5,thumb_len,
      base_revision,evidence,p_attestation_ids,p_audit_id)
    returning occupancy_id into ident;
  insert into public.forward_prospective_photo_binding_20261008
   values(ident,r.id,base_revision,evidence,p_attestation_ids);
  return ident;
end;
$$;
-- Owner-only admission: no service, attester, anon or authenticated path.
revoke all on function public.admit_prospective_photo_occupancy_20261008(uuid,uuid,text,uuid[],uuid)
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006;
grant execute on function public.admit_prospective_photo_occupancy_20261008(uuid,uuid,text,uuid[],uuid)
  to fixer_forward_media_owner_20261006;

-- Tenant-scoped source revocation: every active occupancy for the source
-- becomes terminally revoked. Returns the revoked count (0 allowed).
-- Callable while the gate is OFF (it only reduces occupancy). Never deletes.
create function public.revoke_prospective_photo_occupancy_20261008(
  p_tenant_id text,p_source_sha256 text,p_reason text
) returns integer language plpgsql security definer set search_path=pg_catalog,public as $$
declare n integer;
begin
  if nullif(btrim(p_tenant_id),'') is null or p_source_sha256 is null
      or p_source_sha256 !~ '^[0-9a-f]{64}$' or nullif(btrim(p_reason),'') is null then
    raise exception 'exact prospective revocation binding required' using errcode='22023';
  end if;
  if current_setting('transaction_isolation')<>'read committed' then
    raise exception 'forward media authority requires read committed isolation' using errcode='25000';
  end if;
  perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
  update public.forward_prospective_photo_occupancy_20261008
    set state='revoked', state_reason=p_reason, state_changed_at=now()
    where state='active' and tenant_id=btrim(p_tenant_id) and source_sha256=p_source_sha256;
  get diagnostics n=row_count;
  return n;
end;
$$;
revoke all on function public.revoke_prospective_photo_occupancy_20261008(text,text,text)
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006;
grant execute on function public.revoke_prospective_photo_occupancy_20261008(text,text,text)
  to fixer_forward_media_owner_20261006;

-- Consult-the-exact-occupancy proof for the reservation/provenance/
-- finalization/claim boundaries. Only an ACTIVE occupancy matching the row's
-- current tenant, post_date, validated logical post, source SHA and CURRENT
-- revision resolves. The returned receipt is EXPLICITLY lane-tagged
-- 'prospective_occupancy': it is not a publish claim, not a reservation and
-- not historical clearance evidence; every boundary that consumes it must
-- distinguish it from the historical lane (whose no-exclusions guard remains
-- in force and is never satisfied or bypassed by this receipt).
create function public.forward_prospective_photo_proof_20261008(
  p_calendar_row_id uuid,p_source_sha256 text
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare
  r public.content_calendar%rowtype;
  tenant text;
  x public.forward_prospective_photo_occupancy_20261008%rowtype;
  binding public.forward_prospective_photo_binding_20261008%rowtype;
  revision text;
begin
  if p_calendar_row_id is null or p_source_sha256 is null or p_source_sha256 !~ '^[0-9a-f]{64}$' then
    raise exception 'exact prospective proof binding required' using errcode='22023';
  end if;
  select * into r from public.content_calendar where id=p_calendar_row_id;
  if not found or r.logical_post_id is null or r.post_date is null or nullif(btrim(r.gym_id),'') is null then
    raise exception 'prospective occupancy proof unavailable' using errcode='23514';
  end if;
  select a.tenant_id into tenant from public.fixer_forward_media_tenant_alias_20261006 a
    where a.alias_key=btrim(r.gym_id);
  tenant:=coalesce(tenant,btrim(r.gym_id));
  select * into x from public.forward_prospective_photo_occupancy_20261008 s
    where s.state='active' and s.tenant_id=tenant and s.post_date=r.post_date
      and s.logical_post_id=r.logical_post_id and s.source_sha256=p_source_sha256;
  if not found then
    raise exception 'prospective occupancy proof unavailable' using errcode='23514';
  end if;
  revision:=public.fixer_forward_media_attestation_request_20261006(r.id)->>'revision';
  select * into binding from public.forward_prospective_photo_binding_20261008 b
   where b.occupancy_id=x.occupancy_id and b.calendar_row_id=r.id and b.row_revision=revision;
  if not found then raise exception 'current sibling prospective proof unavailable' using errcode='23514'; end if;
  return jsonb_build_object('lane','prospective_occupancy','occupancy_id',x.occupancy_id,
    'tenant_id',x.tenant_id,'post_date',x.post_date,'logical_post_id',x.logical_post_id,
    'source_sha256',x.source_sha256,'audit_id',x.audit_id,
    'row_revision',binding.row_revision,'lineage_evidence_id',binding.lineage_evidence_id,
    'attestation_ids',binding.attestation_ids);
end;
$$;
revoke all on function public.forward_prospective_photo_proof_20261008(uuid,text)
  from public,anon,authenticated,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant execute on function public.forward_prospective_photo_proof_20261008(uuid,text)
  to service_role,fixer_forward_media_owner_20261006;

commit;
