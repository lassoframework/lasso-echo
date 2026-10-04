-- DRAFT / UNAPPLIED / INCOMPLETE — DO NOT APPLY, DO NOT ACTIVATE.
--
-- STATUS: INCOMPLETE — Astra review 2026-10-03
-- ---------------------------------------------------------------------------
-- The prep-time writer architecture this file was drafted for was REJECTED:
-- recording a scene at preparation time marks UNUSED candidates as used, the
-- record is not atomic with the real visual_global_usage claim (which happens
-- later, in the calendar-trigger visual_global_claim_scene transaction), and
-- it still races across gyms. The functions below are therefore NOT a valid
-- production write path. This file survives only as a design sketch.
--
-- Required redesign BEFORE anything here may ever be applied or activated:
--   (a) A candidate/staging table written at prep that does NOT count as used
--       (preparation evidence must never consume a scene).
--   (b) Scene recording and the occupied-scene/dates comparison moved INTO
--       the claim transaction (the visual_global_claim_scene path) under its
--       existing locks, owner-attested with the exact object bytes in the
--       same call that establishes byte authority.
--   (c) A durable review-hold table written atomically in that same claim
--       transaction whenever a conflict is found.
--   (d) Hamming comparison of ALL occupied scenes/dates enforced
--       transactionally, server-side, never by the client.
--   (e) Full backfill before any activation.
--
-- pHash is SIMILARITY EVIDENCE, NEVER byte identity. A scene match can only
-- ever route a candidate to manual review; it never auto-approves and never
-- writes visual_group_scene_link, which remains HUMAN-CONFIRMED ONLY via
-- visual_group_link_scene. The 7..30 candidate band is calibrated on exactly
-- one measured incident pair (Swift River JCK_6328/JCK_6331, hamming 28) and
-- is hold-only.
--
-- Rollback: nothing here is applied anywhere; delete the file. The frozen
-- md5-keyed exact-byte ledger (visual_global_usage) is untouched.

-- ---------------------------------------------------------------------------
-- TABLE SKETCH ONLY (not applied; shape subject to the redesign above).
-- Global cross-tenant scene (pHash) similarity evidence. used_date is NOT
-- NULL so guard reads never face a missing date dimension.
-- ---------------------------------------------------------------------------
create table if not exists public.visual_scene_phash (
  phash      char(16)    not null check (phash ~ '^[0-9a-f]{16}$'),
  tenant_id  uuid        not null,
  group_key  text        not null,
  used_date  date        not null,
  evidence   jsonb       not null default '{}',
  created_by text        not null,
  created_at timestamptz not null default now(),
  primary key (phash, tenant_id, group_key)
);

comment on table public.visual_scene_phash is
  'DESIGN SKETCH, INCOMPLETE (Astra review 2026-10-03): append-only global pHash scene evidence per canonical tenant+group+date. Similarity evidence only, never byte identity; matches route to manual review and must never auto-write visual_group_scene_link. Not a valid write target until the claim-transaction redesign lands.';

comment on column public.visual_scene_phash.used_date is
  'The reservation/publication date the scene was used on. NOT NULL so guard reads never face a missing date dimension; drives the same-tenant cross-date hold policy in the selector.';

-- Cross-tenant hamming scans: the guard scans every known pHash regardless of
-- tenant (a reused scene must be held across gyms AND across dates), then
-- distances are computed by the caller over the candidate rows.
create index if not exists visual_scene_phash_phash_idx
  on public.visual_scene_phash (phash);

-- Date-dimension scans for the same-tenant cross-date hold policy.
create index if not exists visual_scene_phash_tenant_date_idx
  on public.visual_scene_phash (tenant_id, used_date);

-- Append-only: similarity evidence is history. No UPDATE or DELETE, in the
-- same immutable-identity style as the visual_group identity tables.
create or replace function public.visual_scene_phash_block_mutation()
returns trigger language plpgsql set search_path = public as $$
begin
  raise exception 'visual scene evidence is append-only and immutable' using errcode='23514';
end;
$$;

drop trigger if exists visual_scene_phash_immutable on public.visual_scene_phash;
create trigger visual_scene_phash_immutable
  before update or delete on public.visual_scene_phash
  for each row execute function public.visual_scene_phash_block_mutation();

-- ---------------------------------------------------------------------------
-- REJECTED WRITE PATHS — kept for reference only, NOT valid production
-- writers (Astra review 2026-10-03). visual_scene_record_use (security
-- definer, service-role) and visual_scene_record_use_tx (security invoker,
-- granted to no role) both record at PREPARATION time: they would mark
-- unused candidates as used and can never be atomic with the later
-- visual_global_claim_scene usage claim. Do not call either from any guarded
-- path; the redesign above replaces them with in-claim-transaction recording.
-- Both validate the namespaced 'scene:phash64:<16hex>' format, require
-- tenant/group/date, and use ON CONFLICT DO NOTHING (append-only, consistent
-- with the immutability trigger). Neither ever writes visual_group_scene_link.
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_record_use(
  p_phash text, p_tenant_id uuid, p_group_key text,
  p_used_date date, p_evidence jsonb
) returns void language plpgsql security definer set search_path = public as $$
declare v_phash text;
begin
  raise exception 'visual_scene_record_use is a REJECTED prep-time write path (Astra review 2026-10-03); scene recording must move into the claim transaction before this file is applied' using errcode='0A000';
end;
$$;

create or replace function public.visual_scene_record_use_tx(
  p_phash text, p_tenant_id uuid, p_group_key text,
  p_used_date date, p_evidence jsonb
) returns void language plpgsql security invoker set search_path = public as $$
begin
  raise exception 'visual_scene_record_use_tx is a REJECTED prep-time write path (Astra review 2026-10-03); scene recording must move into the claim transaction before this file is applied' using errcode='0A000';
end;
$$;

-- Service-role read only, matching every other visual ledger table. Even
-- these grants are moot while the file is unapplied; they exist so the sketch
-- reads as the shape the redesign will start from.
alter table public.visual_scene_phash enable row level security;
drop policy if exists visual_scene_phash_service_role on public.visual_scene_phash;
create policy visual_scene_phash_service_role on public.visual_scene_phash
  for all to service_role using (true) with check (true);
revoke all on public.visual_scene_phash from public,anon,authenticated,service_role;
grant select on public.visual_scene_phash to service_role;
revoke all on function public.visual_scene_phash_block_mutation() from public,anon,authenticated;
revoke all on function public.visual_scene_record_use(text,uuid,text,date,jsonb) from public,anon,authenticated;
grant execute on function public.visual_scene_record_use(text,uuid,text,date,jsonb) to service_role;
revoke all on function public.visual_scene_record_use_tx(text,uuid,text,date,jsonb)
  from public,anon,authenticated,service_role;
