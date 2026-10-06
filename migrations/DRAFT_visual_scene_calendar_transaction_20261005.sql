-- DRAFT / UNAPPLIED / OFF. Transaction wiring only. NEVER APPLY TO PRODUCTION.
-- Requires the group schema/claim trigger/global history/backfill/activation
-- drafts, DRAFT_visual_scene_ledger_20261005.sql, and
-- calendar_approval_provenance_20261005.sql before this migration.
-- Replaces the ONE existing calendar BEFORE function; installs no second trigger,
-- flips no setting, performs no backfill, and adds no arming barrier.
-- Unknown historical scene coverage and the raising backfill stub remain RELEASE
-- BLOCKERS, alongside cloud Ultra Review and independent acceptance.
-- GBP and legacy direct UPDATE lanes use this same authoritative BEFORE path.
-- The canonical approval and claim RPCs inspect the trigger's persisted row;
-- direct patch callers must also inspect it before sending.
-- Approval of a review hold alone never reactivates a calendar row. An explicit
-- unsent reactivation is scanned again, with only exact approved-pair exemptions.
-- Rollback: in scratch only, restore visual_group_guard_trigger from the group
-- claim-trigger draft and the approval, slot-claim and GBP-claim RPCs from
-- their preceding definitions. Restore
-- code inside a transaction and preserve all permanent occupancy/hold evidence.
-- A transaction rollback removes both scene and exact-byte writes and the row.
begin;
create or replace function public.visual_group_guard_trigger()
returns trigger language plpgsql security definer set search_path = public as $$
declare resolved text; need_claim boolean; finalized boolean; identity_changed boolean; media_changed boolean; old_tenant text; new_tenant text; scene_scan record; scene_blocked boolean := false;
begin
  select null::text o_tenant,null::uuid o_candidate_id,null::char(16) o_phash,
    null::text o_fingerprint,null::text o_exact_url,null::text o_worst_band,
    '[]'::jsonb o_detail into scene_scan;
  if tg_op='DELETE' then
    if public.visual_group_enforcement_on(old.gym_id) then
      perform public.visual_group_sync_row(old,null,'delete');
    end if;
    return old;
  end if;
  if not public.visual_group_enforcement_on(new.gym_id)
     and (tg_op='INSERT' or not public.visual_group_enforcement_on(old.gym_id)) then
    -- Once any tenant is armed, an unarmed writer cannot create history that
    -- was absent from the global import. Existing untouched rows may remain.
    if exists(select 1 from public.gym_visual_guard_settings where enforce)
       and (new.status='published' or new.published_at is not null
         or public.visual_group_row_active(new) or public.visual_group_row_ambiguous(new))
       and (tg_op='INSERT' or new is distinct from old) then
      raise exception 'unarmed calendar authority requires global history activation first'
        using errcode='23514';
    end if;
    return new;
  end if;
  -- Keep trigger OLD/NEW calendar keys raw. Only internal predicates use
  -- canonical UUIDs; returning NEW must never rewrite a client-facing key.
  new_tenant := public.visual_group_tenant_id(new.gym_id)::text;
  if tg_op='UPDATE' then
    old_tenant := public.visual_group_tenant_id(old.gym_id)::text;
    -- An enforced row cannot escape its authority by changing to an unmapped
    -- or unarmed tenant. Resolve/arm the destination before an unsent move.
    if old_tenant is distinct from new_tenant and public.visual_group_enforcement_on(old.gym_id)
       and not public.visual_group_enforcement_on(new.gym_id) then
      raise exception 'enforced calendar row cannot move to unmapped or unarmed tenant' using errcode='23514';
    end if;
  end if;
  if tg_op='UPDATE' then
    media_changed := new.image_url is distinct from old.image_url or
      exists(select 1 from (values('source_media_url'),('thumbnail_url'),('source_media_asset_id'),('drive_file_id'),('byte_hash'),('r2_key')) x(k)
        where to_jsonb(new)->>k is distinct from to_jsonb(old)->>k);
    identity_changed := new_tenant is distinct from old_tenant or
      new.post_date is distinct from old.post_date or new.visual_group_key is distinct from old.visual_group_key or
      new.image_url is distinct from old.image_url or
      exists(select 1 from (values('source_media_url'),('thumbnail_url'),('source_media_asset_id'),('drive_file_id'),('byte_hash'),('r2_key')) x(k)
        where to_jsonb(new)->>k is distinct from to_jsonb(old)->>k);
    if identity_changed and (new.status='published' or new.published_at is not null or old.status='published' or old.published_at is not null
       or (public.visual_group_row_ambiguous(old) and not public.visual_group_row_reconciled_here(old))) then
      raise exception 'confirmed or ambiguous send identity/date cannot change' using errcode='23514';
    end if;
    if public.visual_group_row_ambiguous(old) and not public.visual_group_row_reconciled_here(old) and exists(
      select 1 from public.visual_group_usage_sibling s where s.gym_id=old_tenant and s.calendar_row_id=old.id and s.ambiguous
        and ((new.publish_claim_token is not null and s.original_claim_token is not null and new.publish_claim_token<>s.original_claim_token)
          or (new.late_post_id is not null and s.original_provider_post_id is not null and new.late_post_id<>s.original_provider_post_id))) then
      raise exception 'ambiguous original claim/provider IDs cannot be replaced' using errcode='23514';
    end if;
    if public.visual_group_row_ambiguous(old) and not public.visual_group_row_reconciled_here(old) and
       (new.variant_status is distinct from old.variant_status or
        (new.published_at is null and new.status not in ('publishing','failed','published'))) then
      raise exception 'ambiguous send needs explicit reconciliation' using errcode='23514';
    end if;
  end if;
  if public.visual_group_enforcement_on(new.gym_id) then
    finalized := new.status='published' or new.published_at is not null;
    need_claim := public.visual_group_row_active(new) or public.visual_group_row_ambiguous(new) or finalized;
    if need_claim then
      -- Take stable identity locks BEFORE readiness reads, not just before
      -- writing the ledger. A concurrent decision event's FK key-share lock
      -- must commit before we re-read whether the candidate remains held.
      resolved:=public.visual_group_resolve_row(new);
      perform public.visual_group_lock_scene_components(jsonb_build_array(
        jsonb_build_object('gym_id',new_tenant,'group_key',new.visual_group_key),
        jsonb_build_object('gym_id',new_tenant,'group_key',resolved),
        jsonb_build_object('gym_id',old_tenant,'group_key',case when tg_op='UPDATE' then old.visual_group_key end)));
      -- Identity hydration while waiting must retry, never add a lower-key
      -- component after retaining the first component's locks.
      if public.visual_group_resolve_row(new) is distinct from resolved then
        raise exception 'visual identity changed while locking; retry transaction' using errcode='55P03';
      end if;
      if finalized and public.visual_group_finalization_requires_evidence(
          case when tg_op='UPDATE' then old end,new)
         and not public.visual_group_finalization_evidenced(
          case when tg_op='UPDATE' then old end,new) then
        raise exception 'ambiguous publication requires terminal provider reconciliation' using errcode='23514';
      end if;
      if tg_op='UPDATE' and media_changed and resolved=old.visual_group_key
         and not exists(
           select 1 from (
             select * from public.visual_group_row_aliases(new)
             except select * from public.visual_group_row_aliases(old)
           ) changed join public.visual_group_alias a
           on a.gym_id=new_tenant and a.alias_kind=changed.alias_kind
              and a.alias_value=changed.alias_value and a.group_key=resolved
         ) then
        -- Carried-forward source aliases are insufficient evidence for newly
        -- changed media. A changed exact alias must independently confirm it.
        resolved:=null;
      end if;
      -- Caller-provided keys are hints, never authority. An unchanged old key
      -- cannot survive a new media URL unless exact aliases still resolve it.
      if new.visual_group_key is not null and new.visual_group_key is distinct from resolved then
        new.visual_group_key := null;
      else new.visual_group_key := resolved; end if;
      if public.visual_group_row_review_pending(new) then
        if new.media_not_ready_reason is null or new.media_not_ready_reason in
          ('visual_group_identity_unresolved','visual_group_date_unresolved','visual_group_scene_review_required') then
          new.media_not_ready_reason:='visual_group_scene_review_required';
        end if;
      elsif new.visual_group_key is null then
        if new.media_not_ready_reason is null or new.media_not_ready_reason in
          ('visual_group_identity_unresolved','visual_group_date_unresolved') then
          new.media_not_ready_reason := 'visual_group_identity_unresolved';
        end if;
      elsif new.post_date is null then
        if new.media_not_ready_reason is null or new.media_not_ready_reason in
          ('visual_group_identity_unresolved','visual_group_date_unresolved') then
          new.media_not_ready_reason := 'visual_group_date_unresolved';
        end if;
      elsif new.media_not_ready_reason in ('visual_group_identity_unresolved','visual_group_date_unresolved','visual_group_scene_review_required') then
        new.media_not_ready_reason := null;
      end if;
      if public.visual_group_row_ambiguous(new) and
         (new.visual_group_key is null or new.post_date is null) and not exists(
           select 1 from public.visual_group_member_event e where e.gym_id=new_tenant
             and e.actor='runtime_ambiguous_review' and e.alias_value=new.id::text
             and not exists(select 1 from public.visual_group_reconciliation r
               where r.gym_id=e.gym_id and r.calendar_row_id=new.id and e.id=any(r.hold_event_ids))) then
        insert into public.visual_group_member_event(gym_id,group_key,alias_value,action,actor,reason)
          values(new_tenant,new.visual_group_key,new.id::text,'review_hold','runtime_ambiguous_review',
            jsonb_build_object('reason','ambiguous_send_identity_or_date_unresolved',
              'calendar_row_id',new.id,'image_url',new.image_url,'post_date',new.post_date,
              'status',new.status,'late_post_id',new.late_post_id,'publish_claim_token',new.publish_claim_token)::text);
      end if;
      -- A held row can be explicitly reactivated only through a fresh scan.
      -- This does not approve a conflict or erase its durable review evidence.
      if new.media_not_ready_reason='scene_review_hold' then
        new.media_not_ready_reason:=null;
      end if;
      -- BEFORE trigger holds must not allow PR230's claim RPC to return a
      -- token after its pre-read. Refuse the entire approval/claim/finalize.
      if (new.status in ('approved','publishing') or finalized) and
        (new.visual_group_key is null or new.post_date is null or
         nullif(btrim(new.image_url),'') is null or new.media_not_ready_reason is not null) then
        raise exception 'visual media not ready for approval, claim or finalize' using errcode='23514';
      end if;
      if new.visual_group_key is not null and new.post_date is not null
          and nullif(btrim(new.image_url),'') is not null
          and new.media_not_ready_reason is null then
        select * into scene_scan from public.visual_scene_claim_scan(
          new,public.visual_scene_row_candidate(new));
        scene_blocked:=scene_scan.o_worst_band is not null;
        if scene_blocked then
          -- Confirmed/uncertain attempts require existing reconciliation;
          -- never erase a provider attempt in order to save a scene hold.
          if finalized or new.late_post_id is not null or
             (tg_op='UPDATE' and public.visual_group_row_ambiguous(old)) then
            raise exception 'scene conflict on confirmed or ambiguous attempt requires reconciliation'
              using errcode='23514';
          end if;
          new.status:='pending';
          new.variant_status:='archived';
          new.media_not_ready_reason:='scene_review_hold';
          new.publish_claim_token:=null;
          new.publish_reservation_day:=null;
        end if;
      end if;
    end if;
  end if;
  perform public.visual_group_sync_row(case when tg_op='UPDATE' then old end,new,lower(tg_op));
  -- sync_row retains all local/global exact-byte validation and locks.
  -- A blocked NEW is inactive, so no new exact-byte use is recorded. Release
  -- of an old unsent reservation remains governed by the original sync path.
  if scene_blocked then
    -- FINAL mutation: nothing that can raise follows this hold insertion.
    -- The caller must commit the held calendar UPDATE/INSERT for durability.
    if scene_scan.o_worst_band <> 'fail_closed' then
      perform public.visual_scene_write_holds(scene_scan.o_tenant,
        new.visual_group_key,new.post_date,new.id,new.account,
        scene_scan.o_candidate_id,scene_scan.o_phash,scene_scan.o_fingerprint,
        scene_scan.o_exact_url,scene_scan.o_detail);
    end if;
  elsif scene_scan.o_candidate_id is not null then
    insert into public.visual_scene_phash_occupied
      (phash,tenant_id,group_key,used_date,fingerprint,calendar_row_id,channel,evidence)
    values(scene_scan.o_phash,scene_scan.o_tenant,new.visual_group_key,new.post_date,
      scene_scan.o_fingerprint,new.id,new.account,
      jsonb_build_object('candidate_id',scene_scan.o_candidate_id,'exact_url',scene_scan.o_exact_url))
    on conflict(phash,tenant_id,group_key,used_date) do nothing;
  end if;
  return new;
end;
$$;

-- Needs-media holds must never be claimable by the atomic publish-slot RPC.
--
-- A Story slot whose reviewed 9:16 render or hosting failed is staged as a
-- durable hold: status 'pending' (mapped from a BLOCKED draft), blank image_url
-- and a media_not_ready_reason. due_rows and mark_publishing already refuse such
-- rows, but claim_calendar_publish_slot_owned is a separate atomic claim path
-- and previously only checked status/published_at/variant_status -- a stale
-- 'pending' or 'approved' hold row could be claimed and published with no media.
--
-- Compose into approval-provenance's one defaulted seven-argument signature.
-- Keep its current-autonomy, proof, capacity and format checks intact.
create or replace function public.claim_calendar_publish_slot_owned(
  p_row_id uuid, p_gym_id text, p_day date, p_timezone text,
  p_capacity integer, p_approved_only boolean,
  p_require_approval_proof boolean default false
) returns uuid
language plpgsql security definer set search_path = public
as $$
declare
  v_row public.content_calendar%rowtype;
  v_used integer;
  v_token uuid;
  v_enforce_proof boolean;
begin
  if p_capacity < 1 or p_capacity > 3 or p_day is null or p_timezone is null
      or nullif(btrim(p_gym_id), '') is null then
    return null;
  end if;
  if p_capacity = 3 and p_gym_id <> 'lasso' then
    return null;
  end if;

  perform pg_advisory_xact_lock(hashtextextended(p_gym_id, 0));
  v_enforce_proof := p_require_approval_proof
                     and not public.calendar_gym_is_autonomous(p_gym_id);
  select * into v_row from public.content_calendar
    where id = p_row_id and gym_id = p_gym_id
      and status in ('pending', 'approved') and published_at is null
      and late_post_id is null and variant_status = 'active'
      -- media hold guards: never claim a row without real, ready media
      and nullif(btrim(coalesce(image_url, '')), '') is not null
      and media_not_ready_reason is null
    for update;
  if not found or (p_approved_only and v_row.status <> 'approved') then
    return null;
  end if;
  if v_enforce_proof and v_row.status <> 'approved' then
    return null;
  end if;
  if p_capacity = 3 and
      coalesce(nullif(lower(btrim(v_row.format)), ''), 'feed') not in ('feed', 'story') then
    return null;
  end if;
  if v_enforce_proof and
      (v_row.approval_kind is distinct from 'human'
       or nullif(btrim(coalesce(v_row.approved_by, '')), '') is null
       or v_row.approved_at is null
       or v_row.approval_digest is null
       or v_row.approval_digest is distinct from public.calendar_approval_digest(v_row)) then
    return null;
  end if;

  select count(*) into v_used from public.content_calendar
    where gym_id = p_gym_id
      and lower(btrim(coalesce(account, ''))) =
          lower(btrim(coalesce(v_row.account, '')))
      and coalesce(nullif(lower(btrim(format)), ''), 'feed') =
          coalesce(nullif(lower(btrim(v_row.format)), ''), 'feed')
      and ((status = 'publishing' and publish_reservation_day = p_day)
           or (status = 'published' and
               (publish_reservation_day = p_day
                or (published_at is not null and
                    (published_at at time zone p_timezone)::date = p_day))));
  if v_used >= p_capacity then
    return null;
  end if;

  v_token := gen_random_uuid();
  update public.content_calendar
    set status = 'publishing', publish_reservation_day = p_day,
        publish_claim_token = v_token
    where id = p_row_id and gym_id = p_gym_id
    returning * into v_row;
  -- UPDATE RETURNING observes the BEFORE trigger's actual persisted NEW.
  -- Return NULL without raising: the held row and hold evidence must commit.
  if not found or v_row.status is distinct from 'publishing'
      or v_row.variant_status is distinct from 'active'
      or v_row.media_not_ready_reason is not null
      or nullif(btrim(coalesce(v_row.image_url, '')), '') is null
      or (v_enforce_proof and
          (v_row.approval_kind is distinct from 'human'
           or nullif(btrim(coalesce(v_row.approved_by, '')), '') is null
           or v_row.approved_at is null
           or v_row.approval_digest is distinct from public.calendar_approval_digest(v_row)))
      or v_row.publish_claim_token is distinct from v_token
      or v_row.publish_reservation_day is distinct from p_day then
    return null;
  end if;
  return v_token;
end;
$$;

revoke all on function public.claim_calendar_publish_slot_owned(uuid, text, date, text, integer, boolean, boolean)
  from public, anon, authenticated;
grant execute on function public.claim_calendar_publish_slot_owned(uuid, text, date, text, integer, boolean, boolean)
  to service_role;


-- Archived content_calendar variants are historical audit records. They may retain
-- pending status and media for forensics, but they must never be approved or claimed.
-- Compose into approval-provenance's one defaulted three-argument RPC.
create or replace function public.approve_calendar_row_if_media_ready(
  p_row_id uuid, p_gym_id text, p_expected jsonb default null
) returns setof public.content_calendar
language sql security definer set search_path = public
as $$
  with persisted as (update public.content_calendar c
     set status = 'approved',
         approval_kind = null,
         approved_by = null,
         approved_at = null,
         approval_digest = public.calendar_approval_digest(c)
   where id = p_row_id and gym_id = p_gym_id
     and status = 'pending' and published_at is null
     and late_post_id is null
     and variant_status = 'active'
     and nullif(btrim(coalesce(image_url, '')), '') is not null
     and media_not_ready_reason is null
     and (p_expected is null or
       (c.caption is not distinct from p_expected->>'caption'
        and c.image_url = p_expected->>'media_url'
        and c.post_date::text = coalesce(p_expected->>'day_key', '')
        and coalesce(nullif(lower(btrim(c.format)), ''), 'feed')
          = coalesce(nullif(lower(btrim(coalesce(p_expected->>'format', ''))), ''), 'feed')
        and coalesce(nullif(lower(btrim(c.account)), ''), '')
          = lower(btrim(coalesce(p_expected->>'platform', '')))
        -- Compose approval-provenance's exact raw GBP card compare. Without
        -- this predicate, the later scene migration would overwrite its CAS.
        and (lower(btrim(c.account)) <> 'googlebusiness'
             or public.calendar_gbp_approval_snapshot(c) = p_expected->'gbp_proof')))
  returning *)
  select * from persisted where status='approved' and variant_status='active'
    and media_not_ready_reason is null and publish_claim_token is null
    and nullif(btrim(coalesce(image_url, '')), '') is not null
    and approval_digest is not distinct from public.calendar_approval_digest(persisted);
$$;

revoke all on function public.approve_calendar_row_if_media_ready(uuid, text, jsonb)
  from public, anon, authenticated;
grant execute on function public.approve_calendar_row_if_media_ready(uuid, text, jsonb)
  to service_role;

-- GBP uses a separate proof-gated claim path in approval-provenance. Its
-- UPDATE RETURNING is also subject to the scene BEFORE trigger. A held row
-- must remain committed but cannot be returned as a successful claim.
create or replace function public.claim_calendar_gbp_publish_owned(
  p_row_id uuid, p_gym_id text
) returns setof public.content_calendar
language plpgsql security definer set search_path = public
as $$
declare
  v_row public.content_calendar%rowtype;
  v_enforce_proof boolean;
  v_token uuid;
begin
  if p_row_id is null or nullif(btrim(coalesce(p_gym_id, '')), '') is null then
    return;
  end if;
  perform pg_advisory_xact_lock(hashtextextended(p_gym_id, 0));
  v_enforce_proof := not public.calendar_gym_is_autonomous(p_gym_id);
  select * into v_row from public.content_calendar c
    where c.id = p_row_id and c.gym_id = p_gym_id
      and c.account = 'googlebusiness' and c.status = 'approved'
      and c.variant_status = 'active' and c.published_at is null
      and c.late_post_id is null and c.publish_claim_token is null
      and c.media_not_ready_reason is null
      and nullif(btrim(coalesce(c.image_url, '')), '') is not null
    for update;
  if not found then return; end if;
  if v_enforce_proof and (
      v_row.approval_kind is distinct from 'human'
      or nullif(btrim(coalesce(v_row.approved_by, '')), '') is null
      or v_row.approved_at is null or v_row.approval_digest is null
      or v_row.approval_digest is distinct from
         public.calendar_approval_digest(v_row)) then
    return;
  end if;
  v_token := gen_random_uuid();
  update public.content_calendar c
    set status = 'publishing', publish_claim_token = v_token
    where c.id = p_row_id and c.gym_id = p_gym_id
    returning c.* into v_row;
  if not found or v_row.status is distinct from 'publishing'
      or v_row.variant_status is distinct from 'active'
      or v_row.media_not_ready_reason is not null
      or nullif(btrim(coalesce(v_row.image_url, '')), '') is null
      or v_row.publish_claim_token is distinct from v_token
      or (v_enforce_proof and
          (v_row.approval_kind is distinct from 'human'
           or nullif(btrim(coalesce(v_row.approved_by, '')), '') is null
           or v_row.approved_at is null
           or v_row.approval_digest is distinct from
              public.calendar_approval_digest(v_row))) then
    return;
  end if;
  return next v_row;
end;
$$;

revoke all on function public.claim_calendar_gbp_publish_owned(uuid, text)
  from public, anon, authenticated;
grant execute on function public.claim_calendar_gbp_publish_owned(uuid, text)
  to service_role;

commit;
