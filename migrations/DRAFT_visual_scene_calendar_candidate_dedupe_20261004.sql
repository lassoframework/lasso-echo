-- DRAFT / UNAPPLIED / OWNER-REVIEW ONLY -- DO NOT APPLY AUTOMATICALLY.
--
-- Reviewed remediation for a duplicate candidate binding that the atomic
-- calendar installer refuses. This path accepts ONLY two candidates with
-- byte-for-byte identical immutable identity and evidence. Conflicting pHash,
-- fingerprint, evidence, actor, tenant, group, role, or exact URL is never
-- merged. The canonical UUID must be the lowest UUID because the frozen row
-- resolver deterministically selects that UUID.
--
-- visual_scene_review_hold.candidate_id is a real FK and part of approval
-- scope. Every affected hold is copied in full to the immutable remediation
-- receipt before its FK is rebound. Trigger bypass is transaction-local DDL;
-- any validation/update/delete failure rolls the trigger state and all data
-- changes back together. This function is owner-only and receives no API-role
-- EXECUTE grant.

begin;

create table if not exists public.visual_scene_candidate_dedupe_receipt (
  receipt_id uuid primary key default gen_random_uuid(),
  canonical_candidate_id uuid not null,
  duplicate_candidate_id uuid not null unique,
  binding jsonb not null check (jsonb_typeof(binding) = 'object'),
  duplicate_candidate jsonb not null check (jsonb_typeof(duplicate_candidate) = 'object'),
  rebound_review_holds jsonb not null check (jsonb_typeof(rebound_review_holds) = 'array'),
  reviewed_by text not null check (btrim(reviewed_by) <> ''),
  review_evidence jsonb not null check (jsonb_typeof(review_evidence) = 'object'
                                        and review_evidence <> '{}'::jsonb),
  reviewed_at timestamptz not null default now()
);

revoke all on public.visual_scene_candidate_dedupe_receipt
  from public, anon, authenticated, service_role;
grant select on public.visual_scene_candidate_dedupe_receipt to service_role;

create or replace function public.visual_scene_review_identical_candidate_duplicate(
  p_canonical_candidate_id uuid,
  p_duplicate_candidate_id uuid,
  p_reviewed_by text,
  p_review_evidence jsonb
) returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_canonical public.visual_scene_candidate%rowtype;
  v_duplicate public.visual_scene_candidate%rowtype;
  v_min_id uuid;
  v_holds jsonb;
  v_receipt uuid;
begin
  if p_canonical_candidate_id is null or p_duplicate_candidate_id is null
      or p_canonical_candidate_id = p_duplicate_candidate_id
      or nullif(btrim(p_reviewed_by), '') is null
      or p_review_evidence is null or jsonb_typeof(p_review_evidence) <> 'object'
      or p_review_evidence = '{}'::jsonb then
    raise exception 'candidate dedupe requires two IDs, reviewer, and review evidence'
      using errcode = '22023';
  end if;

  lock table public.visual_scene_candidate in access exclusive mode;
  lock table public.visual_scene_review_hold in access exclusive mode;

  select * into v_canonical from public.visual_scene_candidate
    where candidate_id = p_canonical_candidate_id;
  select * into v_duplicate from public.visual_scene_candidate
    where candidate_id = p_duplicate_candidate_id;
  if v_canonical.candidate_id is null or v_duplicate.candidate_id is null then
    raise exception 'reviewed candidate dedupe ID is missing' using errcode = 'P0002';
  end if;
  if v_canonical.tenant_id is distinct from v_duplicate.tenant_id
      or v_canonical.group_key is distinct from v_duplicate.group_key
      or v_canonical.object_role is distinct from v_duplicate.object_role
      or v_canonical.exact_url is distinct from v_duplicate.exact_url
      or v_canonical.phash is distinct from v_duplicate.phash
      or v_canonical.fingerprint is distinct from v_duplicate.fingerprint
      or v_canonical.evidence is distinct from v_duplicate.evidence
      or v_canonical.attested_by is distinct from v_duplicate.attested_by then
    raise exception 'candidate dedupe refused: identity or evidence conflicts'
      using errcode = '23514';
  end if;
  select candidate_id into v_min_id from public.visual_scene_candidate
    where tenant_id = v_canonical.tenant_id and group_key = v_canonical.group_key
      and object_role = v_canonical.object_role and exact_url = v_canonical.exact_url
    order by candidate_id limit 1;
  if v_min_id is distinct from p_canonical_candidate_id then
    raise exception 'candidate dedupe canonical ID must be the frozen resolver minimum UUID'
      using errcode = '23514';
  end if;

  select coalesce(jsonb_agg(to_jsonb(h) order by h.hold_id), '[]'::jsonb)
    into v_holds from public.visual_scene_review_hold h
    where h.candidate_id = p_duplicate_candidate_id;

  insert into public.visual_scene_candidate_dedupe_receipt
    (canonical_candidate_id, duplicate_candidate_id, binding,
     duplicate_candidate, rebound_review_holds, reviewed_by, review_evidence)
  values (
    p_canonical_candidate_id, p_duplicate_candidate_id,
    jsonb_build_object('tenant_id', v_canonical.tenant_id,
      'group_key', v_canonical.group_key, 'object_role', v_canonical.object_role,
      'exact_url', v_canonical.exact_url, 'phash', btrim(v_canonical.phash::text),
      'fingerprint', v_canonical.fingerprint,
      'evidence_md5', md5(v_canonical.evidence::text)),
    to_jsonb(v_duplicate), v_holds, btrim(p_reviewed_by), p_review_evidence
  ) returning receipt_id into v_receipt;

  -- The receipt above preserves the exact old hold rows and duplicate
  -- candidate. Rebind only the FK; all reviewed scope fields remain unchanged.
  alter table public.visual_scene_review_hold
    disable trigger visual_scene_review_hold_guard;
  update public.visual_scene_review_hold
    set candidate_id = p_canonical_candidate_id
    where candidate_id = p_duplicate_candidate_id;
  alter table public.visual_scene_review_hold
    enable trigger visual_scene_review_hold_guard;

  alter table public.visual_scene_candidate
    disable trigger visual_scene_candidate_immutable;
  delete from public.visual_scene_candidate
    where candidate_id = p_duplicate_candidate_id;
  alter table public.visual_scene_candidate
    enable trigger visual_scene_candidate_immutable;

  return v_receipt;
end;
$$;

revoke all on function public.visual_scene_review_identical_candidate_duplicate(
  uuid,uuid,text,jsonb) from public, anon, authenticated, service_role;

commit;
