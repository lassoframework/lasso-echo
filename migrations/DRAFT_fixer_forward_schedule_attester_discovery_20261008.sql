-- DRAFT / UNAPPLIED / DEFAULT OFF. No production activation is authorized.
-- Requires DRAFT_fixer_forward_schedule_stage_20261008.sql (stage tables +
-- forward_schedule_preparation_eligible_20261008 predicate),
-- DRAFT_fixer_forward_media_claim_20261006.sql (snapshot RPC + lineage) and
-- DRAFT_fixer_forward_visual_index_20261008.sql (visual attestation roles).
--
-- This draft adds READ-ONLY staged-lane DISCOVERY for the isolated trusted
-- media attester, replacing operator-supplied staged row UUID and
-- row_uuid:lineage_evidence_uuid environment lists as the sole staged path.
-- Two lanes:
--   * ATTESTER PENDING: registered staged members of a NONTERMINAL batch with
--     no committed lineage for the current persisted revision (attestation
--     still owed). A lineage whose revision no longer matches (stale
--     revision) leaves the row in this lane, never in recovery.
--   * VISUAL RECOVERY: registered staged members whose exact SAME-revision
--     lineage exists but is missing one or more visual roles
--     (original/delivered/thumbnail) after a crash. Only the persisted
--     lineage identity is exposed; recovery reuses that lineage and appends
--     only missing roles. No second lineage, claim, publish or approval is
--     ever created from discovery.
-- Every candidate must satisfy public.forward_schedule_preparation_eligible_
-- 20261008 EXACTLY ({eligible:true,mode:'staged',tenant_id,batch_id,
-- reason:null}); the forward_reservation_staged marker alone never admits a
-- row. A forged marker without membership, a tenant mismatch, any post-stage
-- content/media drift, a claim/send/approval and a finalized batch all fail
-- closed to an empty result. The canonical tenant is bound through
-- fixer_forward_media_tenant_alias_20261006.
--
-- These functions EXPOSE candidate identity only and are granted ONLY to the
-- dedicated attester role (service_role, owner and anon/authenticated are
-- excluded; service-role isolation is preserved). Keyset cursors
-- (p_after_row_id) mirror the active-lane pending RPC so a permanently held
-- earlier row cannot starve later ones. Rollback before use: remove the new
-- functions. After use preserve all rows.
begin;

do $$
begin
  if to_regprocedure('public.forward_schedule_preparation_eligible_20261008(uuid)') is null
      or to_regclass('public.forward_schedule_stage_member_20261008') is null
      or to_regclass('public.forward_schedule_stage_batch_20261008') is null then
    raise exception 'forward schedule stage draft is required' using errcode='23514';
  end if;
  if to_regprocedure('public.fixer_forward_media_attestation_request_20261006(uuid)') is null
      or to_regclass('public.fixer_forward_media_lineage_20261006') is null then
    raise exception 'forward media claim draft is required' using errcode='23514';
  end if;
  if to_regclass('public.forward_media_visual_attestation') is null then
    raise exception 'forward visual index draft is required' using errcode='23514';
  end if;
end;
$$;

-- STAGED ATTESTER PENDING: exact staged member rows whose current persisted
-- revision has no committed lineage. Returns the attestation snapshot (the
-- exact shape fixer_forward_media_pending_attestations_20261006 returns for
-- the active lane) plus the batch identity; discovery exposes no media bytes
-- or URLs beyond the persisted snapshot binding.
create function public.fixer_forward_schedule_staged_attester_pending_20261008(
  p_tenant_id text,p_limit integer default 50,p_after_row_id uuid default null
) returns setof jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare revision text;
begin
  if nullif(btrim(p_tenant_id),'') is null or p_limit is null or p_limit<1 or p_limit>100 then
    raise exception 'bounded tenant-scoped attestation request required' using errcode='22023';
  end if;
  return query
    with eligible_members as (
      select r.id
      from public.content_calendar r
      join public.forward_schedule_stage_member_20261008 m on m.calendar_row_id=r.id
      join public.forward_schedule_stage_batch_20261008 b on b.batch_id=m.batch_id and b.state='staged'
      where r.variant_status='candidate' and r.media_not_ready_reason='forward_reservation_staged'
        and r.status in ('pending','draft')
        and r.gym_id=m.gym_id and m.tenant_id=b.tenant_id
        and m.tenant_id=p_tenant_id
        and m.tenant_id=coalesce((select a.tenant_id from public.fixer_forward_media_tenant_alias_20261006 a
           where a.alias_key=btrim(r.gym_id)),btrim(r.gym_id))
        and r.logical_post_id=m.logical_post_id and r.post_date=m.post_date
        and r.source_media_url=m.source_media_url and r.image_url=m.image_url
        and r.thumbnail_url is not distinct from m.thumbnail_url
        and r.publish_claim_token is null and r.published_at is null and r.late_post_id is null
        -- Snapshot preconditions: rows still awaiting owner/binder preparation
        -- (no render_manifest_digest yet) fail closed OUT of discovery instead
        -- of raising inside the snapshot RPC.
        and nullif(btrim(r.gym_id),'') is not null and r.post_date is not null
        and nullif(btrim(r.visual_group_key),'') is not null
        and nullif(btrim(r.source_media_asset_id),'') is not null
        and r.render_manifest_digest ~ '^sha256:[0-9a-f]{64}$'
        and r.source_media_url ~ '^https://[^[:space:]]+$'
        and r.image_url ~ '^https://[^[:space:]]+$'
        and (r.thumbnail_url is null or r.thumbnail_url ~ '^https://[^[:space:]]+$')
        and (p_after_row_id is null or r.id>p_after_row_id)
        and (select public.forward_schedule_preparation_eligible_20261008(r.id))
            = jsonb_build_object('eligible',true,'mode','staged','tenant_id',p_tenant_id,
                                 'batch_id',m.batch_id,'reason',null)
    ),
    -- Registered staged membership alone never fills the page: the SAME-
    -- revision lineage nonexistence predicate runs BEFORE ORDER BY/LIMIT, so
    -- an already-attested earlier member cannot starve a later one that still
    -- owes attestation. The snapshot lateral only ever sees eligible members,
    -- so unprepared rows still fail closed instead of raising.
    candidates as (
      select r.id
      from eligible_members e
      join public.content_calendar r on r.id=e.id
      cross join lateral (select public.fixer_forward_media_attestation_request_20261006(r.id) snapshot) s
      where not exists(select 1 from public.fixer_forward_media_lineage_20261006 l
        where l.calendar_row_id=r.id and l.row_revision=s.snapshot->>'revision')
      order by r.id limit p_limit
    )
    select snapshot||jsonb_build_object('batch_id',m.batch_id)
    from candidates c
    join public.content_calendar r on r.id=c.id
    join public.forward_schedule_stage_member_20261008 m on m.calendar_row_id=r.id
    cross join lateral (select public.fixer_forward_media_attestation_request_20261006(r.id) snapshot) s
    order by r.id;
end;
$$;
revoke all on function public.fixer_forward_schedule_staged_attester_pending_20261008(text,integer,uuid)
  from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006;
grant execute on function public.fixer_forward_schedule_staged_attester_pending_20261008(text,integer,uuid)
  to fixer_forward_media_attester_20261006;

-- STAGED VISUAL RECOVERY: exact staged member rows whose SAME-revision
-- lineage exists but persists fewer than the three visual roles under that
-- lineage. Exposes the persisted lineage identity and the exact missing
-- roles so the attester can resume the SAME lineage; it never exposes or
-- creates a second lineage and never grants claim/publish eligibility.
create function public.fixer_forward_schedule_staged_visual_recovery_pending_20261008(
  p_tenant_id text,p_limit integer default 50,p_after_row_id uuid default null
) returns setof jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
begin
  if nullif(btrim(p_tenant_id),'') is null or p_limit is null or p_limit<1 or p_limit>100 then
    raise exception 'bounded tenant-scoped attestation request required' using errcode='22023';
  end if;
  return query
    with eligible_members as (
      select r.id
      from public.content_calendar r
      join public.forward_schedule_stage_member_20261008 m on m.calendar_row_id=r.id
      join public.forward_schedule_stage_batch_20261008 b on b.batch_id=m.batch_id and b.state='staged'
      where r.variant_status='candidate' and r.media_not_ready_reason='forward_reservation_staged'
        and r.status in ('pending','draft')
        and r.gym_id=m.gym_id and m.tenant_id=b.tenant_id
        and m.tenant_id=p_tenant_id
        and m.tenant_id=coalesce((select a.tenant_id from public.fixer_forward_media_tenant_alias_20261006 a
           where a.alias_key=btrim(r.gym_id)),btrim(r.gym_id))
        and r.logical_post_id=m.logical_post_id and r.post_date=m.post_date
        and r.source_media_url=m.source_media_url and r.image_url=m.image_url
        and r.thumbnail_url is not distinct from m.thumbnail_url
        and r.publish_claim_token is null and r.published_at is null and r.late_post_id is null
        and nullif(btrim(r.gym_id),'') is not null and r.post_date is not null
        and nullif(btrim(r.visual_group_key),'') is not null
        and nullif(btrim(r.source_media_asset_id),'') is not null
        and r.render_manifest_digest ~ '^sha256:[0-9a-f]{64}$'
        and r.source_media_url ~ '^https://[^[:space:]]+$'
        and r.image_url ~ '^https://[^[:space:]]+$'
        and (r.thumbnail_url is null or r.thumbnail_url ~ '^https://[^[:space:]]+$')
        and (p_after_row_id is null or r.id>p_after_row_id)
        and (select public.forward_schedule_preparation_eligible_20261008(r.id))
            = jsonb_build_object('eligible',true,'mode','staged','tenant_id',p_tenant_id,
                                 'batch_id',m.batch_id,'reason',null)
    ),
    -- Registered staged membership alone never fills the page: the SAME-
    -- revision lineage existence and missing-role predicates run BEFORE
    -- ORDER BY/LIMIT, so a fully attested earlier member cannot starve a
    -- later one still missing roles. The snapshot lateral only ever sees
    -- eligible members, so unprepared rows still fail closed.
    candidates as (
      select r.id
      from eligible_members e
      join public.content_calendar r on r.id=e.id
      cross join lateral (select public.fixer_forward_media_attestation_request_20261006(r.id) snapshot) s
      join public.fixer_forward_media_lineage_20261006 l
        on l.calendar_row_id=r.id and l.row_revision=s.snapshot->>'revision'
      where (select count(distinct v.role) from public.forward_media_visual_attestation v
        where v.lineage_receipt_id=l.evidence_id
          and v.row_revision=('x'||substr(l.row_revision,1,15))::bit(60)::bigint
          and v.tenant_key=p_tenant_id) < 3
      order by r.id limit p_limit
    )
    select jsonb_build_object('calendar_row_id',r.id,'revision',l.row_revision,
      'tenant_id',p_tenant_id,'batch_id',m.batch_id,'lineage_receipt_id',l.evidence_id,
      'missing_roles',(select coalesce(jsonb_agg(role),'[]'::jsonb) from (
        values('original'),('delivered'),('thumbnail')) roles(role)
        where not exists(select 1 from public.forward_media_visual_attestation v
          where v.lineage_receipt_id=l.evidence_id
            and v.row_revision=('x'||substr(l.row_revision,1,15))::bit(60)::bigint
            and v.tenant_key=p_tenant_id and v.role=roles.role)))
    from candidates c
    join public.content_calendar r on r.id=c.id
    join public.forward_schedule_stage_member_20261008 m on m.calendar_row_id=r.id
    cross join lateral (select public.fixer_forward_media_attestation_request_20261006(r.id) snapshot) s
    join public.fixer_forward_media_lineage_20261006 l
      on l.calendar_row_id=r.id and l.row_revision=s.snapshot->>'revision'
    where (select count(distinct v.role) from public.forward_media_visual_attestation v
      where v.lineage_receipt_id=l.evidence_id
        and v.row_revision=('x'||substr(l.row_revision,1,15))::bit(60)::bigint
        and v.tenant_key=p_tenant_id) < 3
    order by r.id;
end;
$$;
revoke all on function public.fixer_forward_schedule_staged_visual_recovery_pending_20261008(text,integer,uuid)
  from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006;
grant execute on function public.fixer_forward_schedule_staged_visual_recovery_pending_20261008(text,integer,uuid)
  to fixer_forward_media_attester_20261006;
commit;
