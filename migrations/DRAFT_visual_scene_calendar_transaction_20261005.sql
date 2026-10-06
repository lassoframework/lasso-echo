-- DRAFT / UNAPPLIED / OFF. Transaction wiring only. NEVER APPLY TO PRODUCTION.
-- Apply AFTER calendar_approval_provenance_20261005.sql, which supersedes
-- all legacy capacity/three-Story claim definitions. Requires its columns,
-- digest, autonomy resolver and wrappers. Do not apply older RPC migrations
-- after this composition. Requires group schema/claim trigger/global history/backfill/activation
-- drafts and DRAFT_visual_scene_ledger_20261005.sql, in their tested order.
-- Replaces the ONE existing calendar BEFORE function; installs no second trigger,
-- flips no setting and performs no backfill. Scene authority must be installed
-- first; missing/OFF scene settings preserve the existing exact-byte guard.
-- SCRATCH-ONLY APPLICATION BARRIER: this file refuses any network-connected
-- session or database except the explicitly named disposable PG17 test DB.
-- Existing exact-byte activation is NOT a scene activation authority. A reviewed
-- historical occupied backfill and activation are required; the separate
-- DRAFT_visual_scene_activation_authority_20261006.sql supplies DEFAULT OFF.
-- Verified coverage and production rollback are still required before a
-- production replacement may be applied.
-- Unknown historical scene coverage and the raising backfill stub remain RELEASE
-- BLOCKERS, alongside independent production acceptance.
-- GBP and legacy direct UPDATE lanes use this same authoritative BEFORE path.
-- Legacy boolean claim RPCs are not granted new rights; held rows become archived
-- and token-free, so any existing pre-read/direct patch caller MUST inspect the
-- persisted row before sending. A boolean legacy caller alone is not release-safe.
-- Approval of a review hold alone never reactivates a calendar row. An explicit
-- unsent reactivation is scanned again, with only exact approved-pair exemptions.
-- Rollback: in scratch only, restore visual_group_guard_trigger from the group
-- claim-trigger draft and the four RPCs from calendar_approval_provenance. Restore
-- code inside a transaction and preserve all permanent occupancy/hold evidence.
-- A transaction rollback removes both scene and exact-byte writes and the row.
begin;
do $$
begin
  if current_database() <> 'echo_scene_ledger_test' or inet_server_addr() is not null then
    raise exception 'SCRATCH ONLY: scene database activation and historical backfill are unresolved';
  end if;
  if to_regprocedure('public.claim_calendar_publish_slot_owned(uuid,text,date,text,integer,boolean,boolean)') is null
     or to_regprocedure('public.approve_calendar_row_if_media_ready(uuid,text,jsonb)') is null
     or to_regprocedure('public.calendar_approval_digest(public.content_calendar)') is null then
    raise exception 'Apply current calendar approval provenance before scene transaction';
  end if;
  if to_regprocedure('public.visual_scene_enforcement_on(text)') is null then
    raise exception 'Apply default-OFF scene authority before scene transaction';
  end if;
end;
$$;
create or replace function public.visual_group_guard_trigger()
returns trigger language plpgsql security definer set search_path = public as $$
declare resolved text; need_claim boolean; finalized boolean; identity_changed boolean; media_changed boolean; old_tenant text; new_tenant text; scene_scan record; scene_blocked boolean := false; scene_enforced boolean := false;
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
  scene_enforced:=public.visual_scene_enforcement_on(new.gym_id);
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
    if old_tenant is distinct from new_tenant and
       public.visual_scene_enforcement_on(old.gym_id) and not scene_enforced then
      raise exception 'scene-enforced row cannot move to scene-OFF tenant' using errcode='23514';
    end if;
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
      if scene_enforced and new.media_not_ready_reason='scene_review_hold' then
        new.media_not_ready_reason:=null;
      end if;
      -- BEFORE trigger holds must not allow PR230's claim RPC to return a
      -- token after its pre-read. Refuse the entire approval/claim/finalize.
      if (new.status in ('approved','publishing') or finalized) and
        (new.visual_group_key is null or new.post_date is null or
         nullif(btrim(new.image_url),'') is null or new.media_not_ready_reason is not null) then
        raise exception 'visual media not ready for approval, claim or finalize' using errcode='23514';
      end if;
      if scene_enforced and new.visual_group_key is not null and new.post_date is not null
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
          -- A held attempt is no longer an approval of the visible creative.
          -- Reactivation requires a fresh card review and trusted proof stamp.
          new.approval_kind:=null;
          new.approved_by:=null;
          new.approved_at:=null;
          new.approval_digest:=null;
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

-- Compose with the current authoritative RPCs, preserving every approval,
-- autonomy, stale-reservation and dated 5/15 capacity predicate. Only the
-- persisted-NEW result checks differ from calendar_approval_provenance.
-- Remove obsolete signatures even when a prior scene draft recreated them:
-- PostgREST must see ONE defaulted signature for each public RPC.
drop function if exists public.claim_calendar_publish_slot_owned(uuid,text,date,text,integer,boolean);
drop function if exists public.approve_calendar_row_if_media_ready(uuid,text);

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
  v_current_used integer;
  v_backlog_used integer;
  v_token uuid;
  v_enforce_proof boolean;
begin
  if p_capacity is null or p_approved_only is null
      or p_require_approval_proof is null
      or p_capacity < 1 or (p_capacity > 3 and p_capacity not in (5, 15))
      or p_day is null or p_timezone is null
      or nullif(btrim(p_gym_id), '') is null then
    return null;
  end if;
  if p_capacity = 3 and p_gym_id <> 'lasso' then
    return null;
  end if;
  if p_capacity = 5 and not (
      p_gym_id = 'lasso'
      and p_day between date '2026-10-07' and date '2026-10-11'
      and p_timezone = 'America/New_York') then
    return null;
  end if;
  if p_capacity = 15 and not (
      p_gym_id = 'lasso'
      and p_day between date '2026-10-05' and date '2026-10-06'
      and p_timezone = 'America/New_York') then
    return null;
  end if;

  perform pg_advisory_xact_lock(hashtextextended(p_gym_id, 0));

  -- ATOMIC MODE CHECK (armed only when the caller passes TRUE, which Echo does
  -- for every lane behind AGENT_APPROVAL_PROOF): decide Manual vs Autonomous
  -- from the authoritative DB NOW, inside the claim's own transaction and
  -- with a settings-row lock -- never from a stale publisher snapshot. A
  -- completed Auto->Manual flip therefore takes effect at the next claim. Any
  -- resolver ambiguity enforces the proof gate (fail closed).
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
  -- Never reclaim a pending/approved row with an unresolved claim token or
  -- reservation day. This guard applies at every capacity and proof setting.
  if v_row.publish_claim_token is not null
      or v_row.publish_reservation_day is not null then
    return null;
  end if;
  -- A gym the DB says is Manual right now may only publish APPROVED rows,
  -- even if the worker's stale snapshot ran the autonomous lane.
  if v_enforce_proof and v_row.status <> 'approved' then
    return null;
  end if;
  if p_capacity in (5, 15) and
      (v_row.post_date is null
       or nullif(btrim(coalesce(v_row.account, '')), '') is null) then
    return null;
  end if;
  if p_capacity in (3, 5, 15) and
      coalesce(nullif(lower(btrim(v_row.format)), ''), 'feed') not in ('feed', 'story') then
    return null;
  end if;
  if p_capacity = 5 and not (
      v_row.post_date = p_day
      or v_row.post_date between date '2026-10-02' and date '2026-10-05') then
    return null;
  end if;
  if p_capacity = 15 and not (
      v_row.post_date = p_day
      or (v_row.post_date between date '2026-10-02' and date '2026-10-05'
          and v_row.post_date < p_day)) then
    return null;
  end if;

  -- APPROVAL PROOF GATE: a fresh VERIFIED human approval whose digest matches
  -- the LOCKED row. Review defect 1: approval_kind='human' can only be stamped
  -- by calendar_stamp_verified_approval, which requires a nonempty Clerk actor
  -- -- an Echo bearer token alone (approval_kind NULL) NEVER satisfies this
  -- gate. approved_by must be NONEMPTY: proof without a trusted actor is not
  -- proof. The digest binds the FINAL image_url, so changed pixels (auto-fit,
  -- reburn, swap) after the stamp fail closed here.
  if v_enforce_proof then
    if v_row.approval_kind is distinct from 'human'
        or nullif(btrim(coalesce(v_row.approved_by, '')), '') is null
        or v_row.approved_at is null
        or v_row.approval_digest is null
        or v_row.approval_digest is distinct from
           public.calendar_approval_digest(v_row) then
      return null;
    end if;
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

  if p_capacity = 5 then
    -- Three current-day slots plus two outage slots on October 7-11.
    -- The tenant advisory lock serializes both class counts.
    select count(*) filter (where post_date = p_day),
           count(*) filter (where post_date between date '2026-10-02'
                                            and date '2026-10-05')
      into v_current_used, v_backlog_used
      from public.content_calendar
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
    if (v_row.post_date = p_day and v_current_used >= 3)
        or (v_row.post_date <> p_day and v_backlog_used >= 2) then
      return null;
    end if;
  end if;

  if p_capacity = 15 then
    -- Three current-day slots plus twelve older-backlog slots on October 5-6.
    -- A post dated October 5 is current on the fifth, backlog on the sixth.
    select count(*) filter (where post_date = p_day),
           count(*) filter (where post_date between date '2026-10-02'
                                            and date '2026-10-05'
                                and post_date < p_day)
      into v_current_used, v_backlog_used
      from public.content_calendar
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
    if (v_row.post_date = p_day and v_current_used >= 3)
        or (v_row.post_date < p_day and v_backlog_used >= 12) then
      return null;
    end if;
  end if;

  v_token := gen_random_uuid();
  update public.content_calendar
    set status = 'publishing', publish_reservation_day = p_day,
        publish_claim_token = v_token
    where id = p_row_id and gym_id = p_gym_id
    returning * into v_row;
  -- BEFORE may commit a scene hold instead of a claim. Never expose its token.
  -- Returning NULL (without raising) preserves that durable hold transaction.
  if not found or v_row.status is distinct from 'publishing'
      or v_row.variant_status is distinct from 'active'
      or v_row.media_not_ready_reason is not null
      or v_row.publish_claim_token is distinct from v_token
      or v_row.publish_reservation_day is distinct from p_day then
    return null;
  end if;
  return v_token;
end;
$$;

revoke all on function public.claim_calendar_publish_slot_owned(uuid,text,date,text,integer,boolean,boolean) from public, anon, authenticated;
grant execute on function public.claim_calendar_publish_slot_owned(uuid,text,date,text,integer,boolean,boolean) to service_role;

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
     and late_post_id is null and variant_status = 'active'
     and nullif(btrim(coalesce(image_url, '')), '') is not null
     and media_not_ready_reason is null
     and (
       p_expected is null
       or (
         -- PORTAL VISIBLE-CARD SNAPSHOT COMPARE (ECHO_VERIFIED_APPROVAL_PROOF
         -- CONTRACT): the exact card the human tapped. caption is compared
         -- null-safely and without trimming; media_url is the FINAL image_url
         -- string (no fetching -- the bound URL IS the
         -- identity); day_key is the visible post_date; format is the
         -- effective format ('feed' when unset); platform is the canonical
         -- account platform. GBP also compares every raw digest-bound field.
         -- ALL of it is checked in THIS SAME UPDATE that
         -- stamps status+digest: a stale snapshot matches zero rows, flips
         -- nothing and stamps nothing -- Echo answers 409
         -- review_refresh_required and the card re-enters review.
         c.caption is not distinct from p_expected->>'caption'
         and c.image_url = p_expected->>'media_url'
         and c.post_date::text
           = coalesce(p_expected->>'day_key', '')
         and coalesce(nullif(lower(btrim(c.format)), ''), 'feed')
           = coalesce(nullif(lower(btrim(coalesce(p_expected->>'format', ''))), ''), 'feed')
         and coalesce(nullif(lower(btrim(c.account)), ''), '')
           = lower(btrim(coalesce(p_expected->>'platform', '')))
         and (lower(btrim(c.account)) <> 'googlebusiness'
              or public.calendar_gbp_approval_snapshot(c) = p_expected->'gbp_proof')
       )
     )
  returning *)
  select * from persisted where status='approved' and variant_status='active'
    and media_not_ready_reason is null and publish_claim_token is null
    and approval_kind is null and approved_by is null and approved_at is null
    and approval_digest = public.calendar_approval_digest(persisted);
$$;

revoke all on function public.approve_calendar_row_if_media_ready(uuid,text,jsonb) from public, anon, authenticated;
grant execute on function public.approve_calendar_row_if_media_ready(uuid,text,jsonb) to service_role;

create or replace function public.claim_calendar_gbp_publish_owned(
  p_row_id uuid, p_gym_id text
) returns setof public.content_calendar
language plpgsql security definer set search_path = public
as $$
declare
  v_row public.content_calendar%rowtype;
  v_enforce_proof boolean;
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
  return query with persisted as (update public.content_calendar c
    set status = 'publishing', publish_claim_token = gen_random_uuid()
    where c.id = p_row_id returning c.*)
    select * from persisted where status='publishing' and variant_status='active'
      and media_not_ready_reason is null and publish_claim_token is not null;
end;
$$;

revoke all on function public.claim_calendar_gbp_publish_owned(uuid,text) from public, anon, authenticated;
grant execute on function public.claim_calendar_gbp_publish_owned(uuid,text) to service_role;

create or replace function public.calendar_stamp_verified_approval(
  p_gym_id uuid,
  p_calendar_id uuid,
  p_clerk_actor_id text,
  p_echo_approval_digest text
) returns table (id uuid, gym_id uuid, clerk_actor_id text,
                 approval_digest text)
language plpgsql security definer set search_path = public
as $$
declare
  v_actor text := nullif(btrim(coalesce(p_clerk_actor_id, '')), '');
  v_digest text := nullif(btrim(coalesce(p_echo_approval_digest, '')), '');
  v_gym_count integer;
  v_mapping_count integer;
  v_mapped_gym uuid;
  v_row public.content_calendar%rowtype;
begin
  if p_gym_id is null or p_calendar_id is null
      or v_actor is null or v_digest is null then
    return;  -- no trusted actor or no Echo digest: zero rows, no stamp
  end if;

-- Exact gym resolution: the gyms row by id, excluding archived/dup rows.
  select count(*) into v_gym_count
    from public.gyms g
   where g.id = p_gym_id
     and lower(coalesce(g.slug, '')) not like '%archived%'
     and lower(coalesce(g.slug, '')) not like '%-dup%'
     and lower(coalesce(g.name, '')) not like '%archived%'
     and lower(coalesce(g.name, '')) not like '%do not use%';
  if v_gym_count is distinct from 1 then
    return;
  end if;

  -- The server-owned token mapping is the only account-key authority. Count
  -- all rows for this key, so duplicates and cross-tenant aliases fail closed.
  select count(*) into v_mapping_count
    from public.echo_intake_tokens t
    join public.content_calendar c on c.gym_id = t.echo_account_key
   where c.id = p_calendar_id and t.gym_id = p_gym_id;
  if v_mapping_count is distinct from 1 or
      (select count(*) from public.echo_intake_tokens t
        join public.content_calendar c on c.gym_id = t.echo_account_key
       where c.id = p_calendar_id) is distinct from 1 then
    return;
  end if;
  -- Hold the authoritative mapping through the stamp. The unique index above
  -- prevents another token row from taking this key while this row is locked.
  select t.gym_id into v_mapped_gym
    from public.echo_intake_tokens t
    join public.content_calendar c on c.gym_id = t.echo_account_key
   where c.id = p_calendar_id and t.gym_id = p_gym_id
   for share of t;
  if v_mapped_gym is distinct from p_gym_id then
    return;
  end if;

  -- Lock the exact row and require it to belong to THIS gym's canonical
  -- account key, be active/unpublished/approved.
  select * into v_row from public.content_calendar c
   where c.id = p_calendar_id
     and c.status = 'approved' and c.published_at is null
     and c.late_post_id is null and c.variant_status = 'active'
     and exists (select 1 from public.echo_intake_tokens t
                  where t.gym_id = p_gym_id
                    and t.echo_account_key = c.gym_id)
   for update of c;
  if not found then
    return;
  end if;

  -- Exact digest match: stored = recomputed from the locked row = Echo's.
  -- Only an unproved approval may be stamped. Concurrent portal taps must
  -- never replace the first authenticated approver's actor attribution.
  if v_row.approval_kind is not null or v_row.approved_by is not null
      or v_row.approved_at is not null
      or v_row.approval_digest is null
      or v_row.approval_digest is distinct from
         public.calendar_approval_digest(v_row)
      or v_row.approval_digest is distinct from v_digest then
    return;
  end if;

  return query
    with persisted as (update public.content_calendar c
       set approval_kind = 'human',
           approved_by = v_actor,
           approved_at = now()
     where c.id = p_calendar_id
    returning c.*)
    select c.id, p_gym_id, c.approved_by, c.approval_digest from persisted c
      where c.status='approved' and c.variant_status='active'
        and c.media_not_ready_reason is null and c.publish_claim_token is null
        and c.approval_kind='human' and c.approved_by=v_actor
        and c.approved_at is not null and c.approval_digest=v_digest
        and c.approval_digest=public.calendar_approval_digest(c);
end;
$$;

revoke all on function public.calendar_stamp_verified_approval(uuid,uuid,text,text) from public, anon, authenticated;
grant execute on function public.calendar_stamp_verified_approval(uuid,uuid,text,text) to service_role;

commit;
