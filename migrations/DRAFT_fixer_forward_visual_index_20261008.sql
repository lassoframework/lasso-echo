-- DRAFT / UNAPPLIED / DEFAULT OFF. No production activation is authorized.
-- Requires DRAFT_fixer_forward_media_claim_20261006.sql (self-contained claim
-- stack). This draft EXTENDS fixer_claim_forward_media_20261006; it never
-- replaces, bypasses or re-implements its signature, ownership, replay, tenant
-- or approval checks. The base claim RPC is invoked inside the same
-- transaction, so visual attestation proof and byte occupancy commit or roll
-- back atomically as ONE transaction. No locks are held across object fetches:
-- this migration performs no network/object I/O, and trusted attester inserts
-- happen in separate committed transactions before any claim.
-- Lock order (matches the existing stack): graph -> census -> row -> token.
-- Policy: exact SHA-256 ancestry conflicts across dates/gyms BLOCK; pHash v1
-- Hamming <=6 BLOCKS; 7-30 HOLDS for review (incident pair at 28 included);
-- >30 is no match. Same tenant/date/logical-post siblings and exact-token
-- retries may reuse their own visual (identical SHA). Changed row/date/media/
-- token or another tenant may not. A negative appended between preparation and
-- claim (or before replay) blocks. Unknown historical use is a HOLD, never
-- assumed clear: an empty visual index grants no historical clearance.
-- Evidence is immutable and survives deletion, replacement, provider errors
-- and ambiguous sends. Producer-asserted hashes are never accepted: every
-- attestation is bound to trusted-attester lineage/object-read receipts.
-- Rollback before use: remove the new objects. After use preserve evidence.
begin;

-- Hard prerequisite: this draft extends, and therefore requires, the base
-- claim stack. Fail the whole migration if it is absent.
do $$
begin
  if not exists(select 1 from pg_catalog.pg_proc p
    join pg_catalog.pg_namespace n on n.oid=p.pronamespace
    where n.nspname='public' and p.proname='fixer_claim_forward_media_20261006') then
    raise exception 'base forward media claim draft is required' using errcode='23514';
  end if;
end;
$$;

-- Private base implementation retains all existing semantics. The old public
-- signature is an OFF dispatcher only; service callers cannot invoke the
-- implementation when the database visual fence is armed.
alter function public.fixer_claim_forward_media_20261006(uuid,uuid,uuid,text)
  rename to fixer_claim_forward_media_internal_20261008;
revoke all on function public.fixer_claim_forward_media_internal_20261008(uuid,uuid,uuid,text)
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
create table public.forward_media_visual_gate_20261008 (
  singleton boolean primary key default true check(singleton),
  enabled boolean not null default false
);
insert into public.forward_media_visual_gate_20261008 values(true,false);
alter table public.forward_media_visual_gate_20261008 enable row level security;
revoke all on public.forward_media_visual_gate_20261008
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
create function public.fixer_forward_visual_gate_lock_20261008()
returns trigger language plpgsql set search_path=pg_catalog,public as $$
begin
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
  return new;
end;
$$;
revoke all on function public.fixer_forward_visual_gate_lock_20261008() from public;
create trigger visual_gate_lock before insert or update or delete on public.forward_media_visual_gate_20261008
  for each row execute function public.fixer_forward_visual_gate_lock_20261008();
create function public.fixer_claim_forward_media_20261006(
  p_calendar_row_id uuid,p_claim_token uuid,p_evidence_id uuid,p_expected_revision text
) returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
begin
  if current_setting('transaction_isolation')<>'read committed' then
    raise exception 'forward media authority requires read committed isolation' using errcode='25000';
  end if;
  perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
  if not exists(select 1 from public.forward_media_visual_gate_20261008 where singleton and not enabled) then
    raise exception 'visual index armed; protected claim proof required' using errcode='55000';
  end if;
  return public.fixer_claim_forward_media_internal_20261008(p_calendar_row_id,p_claim_token,p_evidence_id,p_expected_revision);
end;
$$;
revoke all on function public.fixer_claim_forward_media_20261006(uuid,uuid,uuid,text)
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant execute on function public.fixer_claim_forward_media_20261006(uuid,uuid,uuid,text) to service_role;

-- A bounded attester read replaces SELECT on the private lineage ledger.
create function public.fixer_forward_visual_receipts_20261008(p_calendar_row_id uuid,p_expected_revision text,p_evidence_id uuid)
returns table(source_read_receipt uuid,image_read_receipt uuid,thumbnail_read_receipt uuid)
language plpgsql security definer set search_path=pg_catalog,public as $$
begin
  if public.fixer_forward_media_attestation_request_20261006(p_calendar_row_id)->>'revision' is distinct from p_expected_revision then
    raise exception 'persisted media revision changed' using errcode='23514';
  end if;
  return query select l.source_read_receipt,l.image_read_receipt,coalesce(l.thumbnail_read_receipt,l.image_read_receipt)
    from public.fixer_forward_media_lineage_20261006 l
    where l.evidence_id=p_evidence_id and l.calendar_row_id=p_calendar_row_id and l.row_revision=p_expected_revision;
end;
$$;
revoke all on function public.fixer_forward_visual_receipts_20261008(uuid,text,uuid)
  from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006;
grant execute on function public.fixer_forward_visual_receipts_20261008(uuid,text,uuid) to fixer_forward_media_attester_20261006;

-- Append-only, immutable visual evidence recorded by the separately
-- provisioned trusted attester role. No UPDATE/DELETE grants exist; the
-- attester role receives INSERT/SELECT only.
create table public.forward_media_visual_attestation (
  attestation_id uuid primary key default gen_random_uuid(),
  tenant_key text not null check (tenant_key=btrim(tenant_key) and tenant_key<>''),
  media_url text not null check (media_url ~ '^https://[^[:space:]]+$'),
  role text not null check (role in ('original','delivered','thumbnail')),
  source_sha256 text not null check (source_sha256 ~ '^[0-9a-f]{64}$'),
  source_md5 text not null check (source_md5 ~ '^[0-9a-f]{32}$'),
  byte_length bigint not null check (byte_length>0 and byte_length<=134217728),
  phash_version integer not null default 1 check (phash_version=1),
  phash_v1 bigint not null,
  -- Bigint projection of the base attestation revision (first 60 bits of the
  -- base md5 revision hex). Binding logic lives in the claim RPC below.
  row_revision bigint not null,
  lineage_receipt_id uuid not null references public.fixer_forward_media_lineage_20261006(evidence_id),
  object_read_receipt_id uuid not null references public.fixer_forward_media_object_read_20261006(receipt_id),
  created_at timestamptz not null default now()
);
create index forward_media_visual_attestation_sha_20261008
  on public.forward_media_visual_attestation(source_sha256);
create index forward_media_visual_attestation_phash_20261008
  on public.forward_media_visual_attestation(phash_v1);

-- Append-only, immutable negative evidence/occupancy. Survives deletion,
-- replacement, provider errors and ambiguous sends; a matching negative
-- appended between preparation and claim (or before replay) blocks.
create table public.forward_media_visual_negative (
  negative_id uuid primary key default gen_random_uuid(),
  tenant_key text not null check (tenant_key=btrim(tenant_key) and tenant_key<>''),
  source_sha256 text check (source_sha256 is null or source_sha256 ~ '^[0-9a-f]{64}$'),
  phash_version integer check (phash_version is null or phash_version=1),
  phash_v1 bigint,
  reason text not null check (btrim(reason)<>''),
  created_at timestamptz not null default now(),
  check (source_sha256 is not null or phash_v1 is not null),
  check ((phash_v1 is null) = (phash_version is null))
);
create index forward_media_visual_negative_sha_20261008
  on public.forward_media_visual_negative(source_sha256);

-- Immutability for both tables, including TRUNCATE and owner accidents.
do $$
declare t text;
begin
  foreach t in array array['forward_media_visual_attestation','forward_media_visual_negative'] loop
    execute format('create trigger immutable_row before update or delete on public.%I for each row execute function public.fixer_forward_media_immutable_20261006()',t);
    execute format('create trigger immutable_truncate before truncate on public.%I for each statement execute function public.fixer_forward_media_immutable_20261006()',t);
    execute format('alter table public.%I enable row level security',t);
    execute format('revoke all on public.%I from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006',t);
    execute format('grant select on public.%I to service_role',t);
    execute format('create policy service_read on public.%I for select to service_role using(true)',t);
  end loop;
end;
$$;

-- Attester: INSERT/SELECT on attestations only. Every attestation append
-- serializes with claims under the exclusive graph lock so a waiting claim
-- observes committed evidence (same pattern as history clearance appends).
create function public.fixer_forward_visual_attestation_lock_20261008()
returns trigger language plpgsql set search_path=pg_catalog,public as $$
begin
  if current_setting('transaction_isolation')<>'read committed' then
    raise exception 'forward media authority requires read committed isolation' using errcode='25000';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0));
  return new;
end;
$$;
revoke all on function public.fixer_forward_visual_attestation_lock_20261008()
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
create trigger attestation_graph_lock before insert on public.forward_media_visual_attestation
  for each row execute function public.fixer_forward_visual_attestation_lock_20261008();
create trigger negative_graph_lock before insert on public.forward_media_visual_negative
  for each row execute function public.fixer_forward_visual_attestation_lock_20261008();

grant select,insert on public.forward_media_visual_attestation to fixer_forward_media_attester_20261006;
create policy attester_read on public.forward_media_visual_attestation
  for select to fixer_forward_media_attester_20261006 using(true);
create policy attester_insert on public.forward_media_visual_attestation
  for insert to fixer_forward_media_attester_20261006 with check(true);
-- Negative evidence is owner-appended only (deletion/replacement/provider
-- error/ambiguous send authority); the attester and service can only read.
grant select,insert on public.forward_media_visual_negative to fixer_forward_media_owner_20261006;
create policy owner_read on public.forward_media_visual_negative
  for select to fixer_forward_media_owner_20261006 using(true);
create policy owner_insert on public.forward_media_visual_negative
  for insert to fixer_forward_media_owner_20261006 with check(true);
grant select on public.forward_media_visual_negative to fixer_forward_media_attester_20261006;
create policy attester_read_negative on public.forward_media_visual_negative
  for select to fixer_forward_media_attester_20261006 using(true);

create function public.fixer_forward_visual_negative_append_20261008(
  p_evidence_id uuid,p_tenant_key text,p_source_sha256 text,p_phash_v1 bigint,p_reason text
) returns uuid language plpgsql security definer set search_path=pg_catalog,public as $$
declare ident uuid;
begin
  if not exists(select 1 from public.fixer_forward_media_lineage_20261006
      where evidence_id=p_evidence_id and tenant_id=p_tenant_key) then
    raise exception 'negative evidence lineage binding invalid' using errcode='23514';
  end if;
  insert into public.forward_media_visual_negative(tenant_key,source_sha256,phash_version,phash_v1,reason)
    values(p_tenant_key,p_source_sha256,case when p_phash_v1 is not null then 1 end,p_phash_v1,p_reason)
    returning negative_id into ident;
  return ident;
end;
$$;
revoke all on function public.fixer_forward_visual_negative_append_20261008(uuid,text,text,bigint,text)
  from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006;
grant execute on function public.fixer_forward_visual_negative_append_20261008(uuid,text,text,bigint,text)
  to fixer_forward_media_attester_20261006;

-- Frozen proof set records ONLY successfully claimed attestations. Preparation
-- appends cannot become occupancy or alter a replay after claim.
create table public.forward_media_visual_claim_proof_20261008 (
  claim_token uuid primary key,
  calendar_row_id uuid not null,
  evidence_id uuid not null,
  row_revision text not null,
  attestation_ids uuid[] not null check(cardinality(attestation_ids)=3),
  created_at timestamptz not null default now()
);
alter table public.forward_media_visual_claim_proof_20261008 enable row level security;
revoke all on public.forward_media_visual_claim_proof_20261008
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
create trigger immutable_row before update or delete on public.forward_media_visual_claim_proof_20261008
  for each row execute function public.fixer_forward_media_immutable_20261006();
create trigger immutable_truncate before truncate on public.forward_media_visual_claim_proof_20261008
  for each statement execute function public.fixer_forward_media_immutable_20261006();

-- Publisher consumes trusted persisted proof only; it never fetches bytes or
-- obtains the isolated attester credentials. A replay returns its frozen set.
create function public.fixer_forward_visual_proof_20261008(
  p_calendar_row_id uuid,p_claim_token uuid,p_evidence_id uuid,p_expected_revision text
) returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare ids uuid[];
begin
  if not exists(select 1 from public.content_calendar r where r.id=p_calendar_row_id
      and r.publish_claim_token=p_claim_token
      and public.fixer_forward_media_attestation_request_20261006(r.id)->>'revision'=p_expected_revision) then
    raise exception 'visual proof row binding invalid' using errcode='23514';
  end if;
  select p.attestation_ids into ids from public.forward_media_visual_claim_proof_20261008 p
    where p.claim_token=p_claim_token and p.calendar_row_id=p_calendar_row_id
      and p.evidence_id=p_evidence_id and p.row_revision=p_expected_revision;
  if ids is null then
    select array_agg(a.attestation_id order by a.role) into ids from (
      select distinct on (v.role) v.role,v.attestation_id
        from public.forward_media_visual_attestation v
        join public.fixer_forward_media_lineage_20261006 l on l.evidence_id=v.lineage_receipt_id
        where l.calendar_row_id=p_calendar_row_id and l.evidence_id=p_evidence_id and l.row_revision=p_expected_revision
        order by v.role,v.created_at desc,v.attestation_id desc
    ) a;
  end if;
  if coalesce(cardinality(ids),0)<>3 then
    raise exception 'visual claim proof unavailable' using errcode='23514';
  end if;
  return jsonb_build_object('attestation_ids',ids);
end;
$$;
revoke all on function public.fixer_forward_visual_proof_20261008(uuid,uuid,uuid,text)
  from public,anon,authenticated,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant execute on function public.fixer_forward_visual_proof_20261008(uuid,uuid,uuid,text) to service_role;

-- Atomic visual-index extension of the base claim. Runs INSIDE the existing
-- claim transaction: it binds persisted row facts to the exact
-- caller outgoing revision/evidence and invokes the private unchanged base
-- so ownership, replay, tenant and approval checks are preserved.
-- SECURITY DEFINER, executable only by the existing claim executor role.
create function public.fixer_forward_visual_index_claim_20261008(
  p_calendar_row_id uuid,p_claim_token uuid,p_evidence_id uuid,p_expected_revision text,p_attestation_ids uuid[]
) returns boolean language plpgsql security definer
set search_path=pg_catalog,public as $$
declare
  r public.content_calendar%rowtype;
  tenant text;
  base_revision text;
  expected_revision bigint;
  evidence uuid;
  replayed boolean;
  att record;
  m record;
  expected_url text;
  dist integer;
  own boolean;
  seen_roles text[]:=array[]::text[];
begin
  if p_calendar_row_id is null or p_claim_token is null or p_evidence_id is null
      or nullif(btrim(p_expected_revision),'') is null or p_attestation_ids is null
      or cardinality(p_attestation_ids)<>3 then
    raise exception 'exact visual claim binding required' using errcode='22023';
  end if;
  if current_setting('transaction_isolation')<>'read committed' then
    raise exception 'forward media authority requires read committed isolation' using errcode='25000';
  end if;
  -- Lock order: graph -> census -> row -> token (the token lock is taken
  -- inside the base claim, after the row lock, matching the existing stack).
  perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
  if not exists(select 1 from public.forward_media_visual_gate_20261008 where singleton and enabled) then
    raise exception 'visual index is OFF pending review' using errcode='55000';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
  -- The claim token binds exactly one owned row; ownership/status/replay
  -- checks themselves remain with the base claim below.
  select * into r from public.content_calendar where id=p_calendar_row_id for update;
  if not found or r.publish_claim_token is distinct from p_claim_token then
    raise exception 'visual claim row ownership binding invalid' using errcode='23514';
  end if;
  select a.tenant_id into tenant from public.fixer_forward_media_tenant_alias_20261006 a
    where a.alias_key=btrim(r.gym_id);
  tenant:=coalesce(tenant,nullif(btrim(r.gym_id),''));
  -- Persisted outgoing revision and its bigint projection for attestations.
  base_revision:=public.fixer_forward_media_attestation_request_20261006(r.id)->>'revision';
  expected_revision:=('x'||substr(base_revision,1,15))::bit(60)::bigint;
  if base_revision is distinct from p_expected_revision then
    raise exception 'outgoing media revision changed' using errcode='23514';
  end if;
  evidence:=p_evidence_id;
  select exists(select 1 from public.fixer_forward_media_claim_receipt_20261006
    where claim_token=p_claim_token) into replayed;
  if exists(select 1 from public.forward_media_visual_claim_proof_20261008 p
      where p.calendar_row_id=r.id and p.claim_token is distinct from p_claim_token) then
    raise exception 'visual owned claim token drift' using errcode='23514';
  end if;
  -- ATOMIC EXTENSION: the base claim remains the sole byte-occupancy,
  -- ownership, replay, tenant, gate, history and approval authority. Its true
  -- result and the visual proof below commit in this ONE transaction.
  if public.fixer_claim_forward_media_internal_20261008(r.id,p_claim_token,evidence,base_revision) is distinct from true then
    raise exception 'atomic forward media authority refused publication' using errcode='23514';
  end if;
  if exists(select 1 from public.forward_media_visual_claim_proof_20261008 p
      where p.claim_token=p_claim_token and (p.calendar_row_id is distinct from r.id
        or p.evidence_id is distinct from evidence or p.row_revision is distinct from base_revision
        or not (p.attestation_ids @> p_attestation_ids and p.attestation_ids <@ p_attestation_ids))) then
    raise exception 'visual claim frozen proof changed' using errcode='23514';
  end if;
  -- Verify every attestation against trusted persisted evidence. Exactly the
  -- three roles, bound to this tenant, exact URLs, current revision, the
  -- claimed lineage and trusted object-read receipts. Producer-asserted
  -- hashes are never accepted: source_md5 must equal the trusted receipt
  -- fingerprint and every id must resolve to real trusted evidence.
  for att in select a.* from public.forward_media_visual_attestation a
    where a.attestation_id=any(p_attestation_ids) loop
    if att.tenant_key is distinct from tenant
        or att.row_revision is distinct from expected_revision
        or att.lineage_receipt_id is distinct from evidence
        or att.role=any(seen_roles) then
      raise exception 'visual attestation evidence invalid' using errcode='23514';
    end if;
    seen_roles:=array_append(seen_roles,att.role);
    expected_url:=case att.role
      when 'original' then r.source_media_url
      when 'delivered' then r.image_url
      else coalesce(r.thumbnail_url,r.image_url) end;
    if expected_url is null or att.media_url is distinct from expected_url then
      raise exception 'visual attestation evidence invalid' using errcode='23514';
    end if;
    if not exists(select 1 from public.fixer_forward_media_object_read_20261006 o
        where o.receipt_id=att.object_read_receipt_id and o.tenant_id=tenant
          and o.exact_url=att.media_url and o.fingerprint='md5:'||att.source_md5
          and o.byte_length=att.byte_length
          and o.receipt_id=(select case att.role when 'original' then l.source_read_receipt
              when 'delivered' then l.image_read_receipt
              else coalesce(l.thumbnail_read_receipt,l.image_read_receipt) end
            from public.fixer_forward_media_lineage_20261006 l where l.evidence_id=evidence)) then
      raise exception 'visual attestation evidence invalid' using errcode='23514';
    end if;
    -- Late negative: appended between preparation and claim (or before
    -- replay) blocks. Exact SHA or near pHash negative evidence fails closed.
    if exists(select 1 from public.forward_media_visual_negative n
        where n.source_sha256=att.source_sha256
          or (n.phash_version=1
            and length(replace(((att.phash_v1 # n.phash_v1)::bit(64))::text,'0',''))<=30)) then
      raise exception 'visual negative evidence blocks claim' using errcode='23514';
    end if;
    -- Unknown historical use is a HOLD, never assumed clear. An empty visual
    -- index grants no clearance: the trusted original receipt must still have
    -- an explicit cleared_unused historical clearance for its exact bytes.
    if att.role='original' and not exists(
        select 1 from public.fixer_forward_media_object_read_20261006 o
        join public.fixer_forward_media_history_clearance_20261006 c
          on c.source_fingerprint=o.fingerprint and c.decision='cleared_unused'
        where o.receipt_id=att.object_read_receipt_id) then
      raise exception 'visual historical use unknown; clearance unavailable' using errcode='23514';
    end if;
    -- Exact SHA-256 ancestry conflicts across tenants/dates/groups block.
    -- Occupancy is defined by a committed base claim receipt: attestations
    -- whose row never claimed are preparation evidence, not occupancy, so
    -- concurrent claimants race on the receipt and at most one succeeds with
    -- no partial occupancy. Receipt context (tenant/date/group) survives
    -- calendar deletion and replacement. Own tenant/date/logical-post matches
    -- are authorized sibling or exact-token retry reuse of the same visual.
    for m in select rec.tenant_id, rec.post_date, rec.group_key
        from public.forward_media_visual_attestation b
        join public.forward_media_visual_claim_proof_20261008 proof on b.attestation_id=any(proof.attestation_ids)
        join public.fixer_forward_media_claim_receipt_20261006 rec on rec.claim_token=proof.claim_token
        where b.source_sha256=att.source_sha256 and b.attestation_id<>all(p_attestation_ids) loop
      if m.tenant_id is distinct from tenant or m.post_date is distinct from r.post_date
          or m.group_key is distinct from r.visual_group_key then
        raise exception 'visual byte ancestry already consumed by another tenant/date/group' using errcode='23514';
      end if;
    end loop;
    -- Versioned pHash v1 similarity against committed visual occupancy.
    -- Hamming <=6 blocks (own-group exact-byte reuse is already exempted via
    -- identical SHA above; own-group near-identical derivatives with
    -- different bytes belong to their authorized logical sibling group).
    -- Across groups, 7-30 holds for review, including the
    -- incident pair at 28. >30 is no match.
    for m in select b.source_sha256, b.phash_v1, rec.tenant_id, rec.post_date, rec.group_key
        from public.forward_media_visual_attestation b
        join public.forward_media_visual_claim_proof_20261008 proof on b.attestation_id=any(proof.attestation_ids)
        join public.fixer_forward_media_claim_receipt_20261006 rec on rec.claim_token=proof.claim_token
        where b.phash_version=1 and b.attestation_id<>all(p_attestation_ids)
          and b.source_sha256 is distinct from att.source_sha256 loop
      dist:=length(replace(((att.phash_v1 # m.phash_v1)::bit(64))::text,'0',''));
      if dist>30 then continue; end if;
      own:=m.tenant_id is not distinct from tenant and m.post_date is not distinct from r.post_date
        and m.group_key is not distinct from r.visual_group_key;
      if own then continue; end if;
      if dist<=6 then
        raise exception 'visual byte ancestry already consumed by another tenant/date/group' using errcode='23514';
      end if;
      raise exception 'visual similarity held for review' using errcode='23514';
    end loop;
  end loop;
  if cardinality(seen_roles)<>3 then
    raise exception 'visual attestation evidence invalid' using errcode='23514';
  end if;
  insert into public.forward_media_visual_claim_proof_20261008(claim_token,calendar_row_id,evidence_id,row_revision,attestation_ids)
    values(p_claim_token,r.id,evidence,base_revision,p_attestation_ids) on conflict(claim_token) do nothing;
  return true;
end;
$$;
-- NOT publicly callable: only the existing claim executor role may run it.
revoke all on function public.fixer_forward_visual_index_claim_20261008(uuid,uuid,uuid,text,uuid[])
  from public,anon,authenticated,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant execute on function public.fixer_forward_visual_index_claim_20261008(uuid,uuid,uuid,text,uuid[])
  to service_role;
commit;
