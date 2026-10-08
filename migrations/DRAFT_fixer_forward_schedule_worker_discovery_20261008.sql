-- DRAFT / UNAPPLIED / DEFAULT OFF. No production activation is authorized.
-- Requires DRAFT_fixer_forward_schedule_stage_20261008.sql (stage tables +
-- forward_schedule_preparation_eligible_20261008 predicate),
-- DRAFT_fixer_forward_media_owner_transport_20261007.sql (owner progress
-- quarantine), DRAFT_fixer_owner_photo_clearance_20261007.sql (photo lane) and
-- the claim/observation chain beneath them.
--
-- This draft adds READ-ONLY staged-lane DISCOVERY for the isolated owner,
-- owner-photo and service binder workers. Every function:
--   * binds the canonical tenant through fixer_forward_media_tenant_alias_20261006,
--   * requires registered NONTERMINAL batch membership
--     (forward_schedule_stage_member_20261008 joined to a state='staged' batch),
--   * re-checks the exact row/revision/observation/content-media binding, and
--   * requires public.forward_schedule_preparation_eligible_20261008 to return
--     EXACTLY {eligible:true, mode:'staged', tenant_id, batch_id, reason:null}.
-- The predicate is the ONLY authorizer; the forward_reservation_staged marker
-- alone never admits a row. A forged marker without membership, a tenant
-- mismatch, any post-stage content/media drift, a claim/send/approval and a
-- finalized batch all fail closed to an empty result.
--
-- These functions EXPOSE candidate identity only. They grant no new write
-- authority: the existing owner reserve/snapshot/locked/record RPCs are
-- variant-agnostic and fail closed on staged rows whose media/source binding
-- is incomplete; fixer_bind_forward_media_manifest_20261006 and
-- fixer_prepare_owner_photo_20261007 remain ACTIVE-ONLY (no staged bind or
-- staged photo grant authority exists here or is created here). The existing
-- active-lane discovery functions are unchanged.
-- Rollback before use: remove the new functions. After use preserve all rows.
begin;

do $$
begin
  if to_regprocedure('public.forward_schedule_preparation_eligible_20261008(uuid)') is null
      or to_regclass('public.forward_schedule_stage_member_20261008') is null
      or to_regclass('public.forward_schedule_stage_batch_20261008') is null then
    raise exception 'forward schedule stage draft is required' using errcode='23514';
  end if;
  if to_regprocedure('public.fixer_forward_media_owner_pending_20261007(text[],integer)') is null then
    raise exception 'forward media owner transport draft is required' using errcode='23514';
  end if;
  if to_regprocedure('public.fixer_owner_photo_pending_20261007(text[],integer)') is null then
    raise exception 'owner photo clearance draft is required' using errcode='23514';
  end if;
end;
$$;

-- The exact authorizing predicate result for a staged row. Anything else
-- (ineligible, wrong mode, tenant or batch drift) excludes the candidate.
create function public.fixer_forward_schedule_staged_authorized_20261008(
  p_calendar_row_id uuid,p_tenant_id text,p_batch_id uuid
) returns boolean language plpgsql security definer set search_path=pg_catalog,public as $$
begin
  return (select e = jsonb_build_object('eligible',true,'mode','staged',
                    'tenant_id',p_tenant_id,'batch_id',p_batch_id,'reason',null)
    from (select public.forward_schedule_preparation_eligible_20261008(p_calendar_row_id) e) x);
end;
$$;
revoke all on function public.fixer_forward_schedule_staged_authorized_20261008(uuid,text,uuid)
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,
  fixer_forward_media_owner_20261006;

-- OWNER staged discovery: exact staged observation candidates for the isolated
-- owner worker. Mirrors fixer_forward_media_owner_pending_20261007 (which stays
-- active-only) but requires registered nonterminal membership, the canonical
-- tenant and the authorizing predicate. Rows already in owner progress
-- (quarantine or final) are excluded exactly like the active lane.
create function public.fixer_forward_schedule_staged_owner_pending_20261008(p_tenants text[],p_limit integer)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if p_tenants is null or cardinality(p_tenants) not between 1 and 32
     or p_limit is null or p_limit not between 1 and 100 then
   raise exception 'bounded owner tenant scope required' using errcode='23514';
 end if;
 return coalesce((select jsonb_agg(candidate order by tenant_rank,recorded_at,calendar_row_id)
 from (select jsonb_build_object('calendar_row_id',o.calendar_row_id,
       'revision',o.row_revision,'observation_digest',o.observation_digest,
       'tenant_id',o.tenant_id,'batch_id',m.batch_id) candidate,
       o.recorded_at,o.calendar_row_id,
       row_number() over(partition by o.tenant_id order by o.recorded_at,o.calendar_row_id) tenant_rank
   from public.fixer_forward_media_observation_20261007 o
   join public.content_calendar r on r.id=o.calendar_row_id
   join public.forward_schedule_stage_member_20261008 m on m.calendar_row_id=r.id
   join public.forward_schedule_stage_batch_20261008 b on b.batch_id=m.batch_id and b.state='staged'
   where r.variant_status='candidate' and r.media_not_ready_reason='forward_reservation_staged'
     and r.status in ('pending','draft')
     and r.gym_id=m.gym_id and o.tenant_id=m.tenant_id and m.tenant_id=b.tenant_id
     and o.tenant_id=any(p_tenants)
     and o.tenant_id=coalesce((select a.tenant_id from public.fixer_forward_media_tenant_alias_20261006 a
        where a.alias_key=btrim(r.gym_id)),btrim(r.gym_id))
     and o.row_revision=md5(to_jsonb(r)::text)
     and o.source_asset_id is not null and o.source_asset_id=r.source_media_asset_id
     and r.logical_post_id=m.logical_post_id and r.post_date=m.post_date
     and r.source_media_url=m.source_media_url and r.image_url=m.image_url
     and r.thumbnail_url is not distinct from m.thumbnail_url
     and o.source_exact_url=m.source_media_url and o.delivered_exact_url=m.image_url
     and r.publish_claim_token is null and r.published_at is null and r.late_post_id is null
     and not exists(select 1 from public.fixer_forward_media_owner_progress_20261007 p
       where p.calendar_row_id=o.calendar_row_id and p.row_revision=o.row_revision
         and p.observation_digest=o.observation_digest)
     and public.fixer_forward_schedule_staged_authorized_20261008(r.id,o.tenant_id,m.batch_id)
   order by tenant_rank,o.recorded_at,o.calendar_row_id limit p_limit) q),'[]'::jsonb);
end;
$$;
revoke all on function public.fixer_forward_schedule_staged_owner_pending_20261008(text[],integer)
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006;
grant execute on function public.fixer_forward_schedule_staged_owner_pending_20261008(text[],integer)
  to fixer_forward_media_owner_20261006;

-- SERVICE binder staged discovery: staged members whose exact immutable owner
-- authority (verified original registry + cleared history + matching render
-- manifest for the member's exact media binding) exists while the calendar row
-- still has no render_manifest_digest. Discovery only: no staged bind RPC
-- exists and this draft creates none; finalize-time binding must re-verify
-- everything against the persisted membership.
create function public.fixer_forward_schedule_staged_binder_pending_20261008(
 p_tenants text[],p_limit integer,p_after_post_date date default null,p_after_row_id uuid default null)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 if p_tenants is null or cardinality(p_tenants) not between 1 and 32
     or p_limit is null or p_limit not between 1 and 100 then
   raise exception 'bounded binder tenant scope required' using errcode='23514';
 end if;
 if (p_after_post_date is null)<>(p_after_row_id is null) then
   raise exception 'binder keyset cursor requires both post date and row id' using errcode='23514';
 end if;
 return coalesce((select jsonb_agg(candidate order by tenant_rank,post_date,calendar_row_id)
 from (select jsonb_build_object('calendar_row_id',r.id,'batch_id',m.batch_id,
       'tenant_id',m.tenant_id,'post_date',r.post_date,'manifest_digest',mf.manifest_digest) candidate,
       r.post_date,r.id calendar_row_id,
       row_number() over(partition by m.tenant_id order by r.post_date,r.id) tenant_rank
   from public.forward_schedule_stage_member_20261008 m
   join public.forward_schedule_stage_batch_20261008 b on b.batch_id=m.batch_id and b.state='staged'
   join public.content_calendar r on r.id=m.calendar_row_id
   join public.fixer_forward_media_original_registry_20261006 a
     on a.tenant_id=btrim(r.gym_id) and a.source_asset_id=r.source_media_asset_id
     and a.source_url=m.source_media_url
   join public.fixer_forward_media_history_clearance_20261006 h
     on h.tenant_id=a.tenant_id and h.source_asset_id=a.source_asset_id
     and h.decision='cleared_unused'
   join public.fixer_forward_media_render_manifest_20261006 mf
     on mf.tenant_id=a.tenant_id and mf.source_asset_id=a.source_asset_id
     and mf.image_url=m.image_url and mf.thumbnail_url is not distinct from m.thumbnail_url
   where r.variant_status='candidate' and r.media_not_ready_reason='forward_reservation_staged'
     and r.status in ('pending','draft')
     and r.gym_id=m.gym_id and m.tenant_id=b.tenant_id
     and m.tenant_id=any(p_tenants)
     and m.tenant_id=coalesce((select al.tenant_id from public.fixer_forward_media_tenant_alias_20261006 al
        where al.alias_key=btrim(r.gym_id)),btrim(r.gym_id))
     and r.logical_post_id=m.logical_post_id and r.post_date=m.post_date
     and r.source_media_url=m.source_media_url and r.image_url=m.image_url
     and r.thumbnail_url is not distinct from m.thumbnail_url
     and r.render_manifest_digest is null
     and r.publish_claim_token is null and r.published_at is null and r.late_post_id is null
     -- Definitive authority refusals are filtered BEFORE the limit: the bind
     -- RPC refuses any source whose fingerprint carries a non-cleared fleet
     -- clearance (e.g. a late fleet hold_uncertain). Selecting such a row
     -- under a tight limit would starve later valid candidates every pass.
     -- The hold receipt itself is preserved; repaired evidence (a genuine
     -- cleared decision once uncertainty resolves) re-admits the row.
     and not exists(select 1 from public.fixer_forward_media_history_clearance_20261006 hx
        where hx.source_fingerprint=a.source_fingerprint and hx.decision<>'cleared_unused')
     and public.fixer_forward_schedule_staged_authorized_20261008(r.id,m.tenant_id,m.batch_id)
     -- Bounded per-tenant keyset progress: callers pass the last attempted
     -- (post_date,row_id) for a single-tenant scan so a held row is advanced
     -- past instead of re-selected forever; an exhausted tenant wraps by
     -- calling again with a null cursor so repaired evidence is revisited.
     and (p_after_row_id is null
          or (r.post_date,r.id) > (p_after_post_date,p_after_row_id))
   order by tenant_rank,r.post_date,r.id limit p_limit) q),'[]'::jsonb);
end;
$$;
revoke all on function public.fixer_forward_schedule_staged_binder_pending_20261008(text[],integer,date,uuid)
  from public,anon,authenticated,fixer_forward_media_attester_20261006,fixer_forward_media_owner_20261006;
grant execute on function public.fixer_forward_schedule_staged_binder_pending_20261008(text[],integer,date,uuid)
  to service_role;

-- OWNER PHOTO staged discovery: mirrors fixer_owner_photo_pending_20261007
-- (which stays active-only) for staged members holding a complete signed
-- certificate chain. The signed certificate binds the RAW gym key (the photo
-- record RPC requires candidate tenant = content_calendar.gym_id) while batch
-- membership binds the canonical tenant; the alias resolution clause above
-- requires both to agree. The authorizing predicate replaces the active-state
-- checks. fixer_prepare_owner_photo_20261007 remains active-only; the staged
-- grant authority is the separately reviewed
-- DRAFT_fixer_forward_schedule_staged_preparation_20261008.sql.
create function public.fixer_forward_schedule_staged_photo_pending_20261008(p_tenants text[],p_limit integer)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare snap jsonb;
begin
 perform public.fixer_assert_owner_photo_runtime_20261007();
 if p_tenants is null or cardinality(p_tenants) not between 1 and 32 or p_limit is null or p_limit not between 1 and 100 then
  raise exception 'bounded owner photo tenant scope required' using errcode='23514'; end if;
 snap:=public.fixer_forward_media_photo_snapshot_20261007();
 return coalesce((select jsonb_agg(item order by tenant_rank,audit_id) from (
  select jsonb_build_object('audit_id',c.audit_id,'calendar_row_id',r.id,'revision',md5(to_jsonb(r)::text),
   'tenant_id',m.tenant_id,'batch_id',m.batch_id,
   'recipe',o.observation_json::jsonb->'recipe') item,c.audit_id,
   row_number() over(partition by m.tenant_id order by c.audit_id) tenant_rank
  from public.fixer_forward_media_photo_certificate_20261007 c
  join public.fixer_forward_media_photo_key_20261007 k on k.key_id=c.key_id
  join public.fixer_forward_media_photo_policy_20261007 policy on policy.policy_id=k.policy_id
  join public.fixer_forward_media_photo_state_20261007 state on state.singleton
  join public.content_calendar r on r.id=c.calendar_row_id
  join public.forward_schedule_stage_member_20261008 m on m.calendar_row_id=r.id
  join public.forward_schedule_stage_batch_20261008 b on b.batch_id=m.batch_id and b.state='staged'
  join public.fixer_forward_media_source_receipt_20261007 src
    on src.receipt_ref=c.payload_json::jsonb#>>'{candidate,source_receipt_ref}'
  join public.fixer_forward_media_observation_20261007 o
    on o.calendar_row_id=r.id and o.row_revision=src.row_revision
  where m.tenant_id=any(p_tenants) and r.gym_id=c.payload_json::jsonb#>>'{candidate,tenant_id}'
   and m.tenant_id=b.tenant_id and o.tenant_id=m.tenant_id and r.gym_id=m.gym_id
   and m.tenant_id=coalesce((select al.tenant_id from public.fixer_forward_media_tenant_alias_20261006 al
      where al.alias_key=btrim(r.gym_id)),btrim(r.gym_id))
   and k.approved and policy.approved and state.enabled and nullif(btrim(state.routes_reconciled_ref),'') is not null
   and c.baseline_id=state.baseline_id and c.generation=state.generation
   and c.spine_digest=snap->>'spine_digest'
   and not exists(select 1 from public.fixer_forward_media_photo_key_revocation_20261007 v where v.key_id=k.key_id)
   and r.variant_status='candidate' and r.media_not_ready_reason='forward_reservation_staged'
   and r.status in ('pending','draft')
   and r.publish_claim_token is null and r.published_at is null and r.late_post_id is null
   and r.render_manifest_digest is null and r.thumbnail_url is not distinct from c.payload_json::jsonb#>>'{candidate,thumbnail_url}'
   and r.logical_post_id=m.logical_post_id and r.post_date=m.post_date
   and r.source_media_url=m.source_media_url and r.image_url=m.image_url
   and r.thumbnail_url is not distinct from m.thumbnail_url
   and public.fixer_owner_photo_source_ready_20261007(src.source_asset_id,src.source_sha256)
   and not exists(select 1 from public.fixer_owner_photo_progress_20261007 p where p.audit_id=c.audit_id)
   and not exists(select 1 from public.fixer_owner_photo_reservation_20261007 p where p.audit_id=c.audit_id)
   and public.fixer_forward_schedule_staged_authorized_20261008(r.id,m.tenant_id,m.batch_id)
  order by tenant_rank,c.audit_id limit p_limit) candidates),'[]'::jsonb);
end; $$;
revoke all on function public.fixer_forward_schedule_staged_photo_pending_20261008(text[],integer)
  from public,anon,authenticated,service_role,fixer_forward_media_attester_20261006,
  fixer_forward_media_photo_auditor_20261007;
grant execute on function public.fixer_forward_schedule_staged_photo_pending_20261008(text[],integer)
  to fixer_forward_media_owner_20261006;
commit;
