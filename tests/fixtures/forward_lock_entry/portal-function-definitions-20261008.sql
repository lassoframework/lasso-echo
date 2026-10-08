-- READ-ONLY PRODUCTION FUNCTION SNAPSHOT, project ooqcvmcjspeltuuhcvlh, 2026-10-08. SOURCE DATA; do not apply as a migration.

-- approve_calendar_row_if_media_ready
CREATE OR REPLACE FUNCTION public.approve_calendar_row_if_media_ready(p_row_id uuid, p_gym_id text, p_expected jsonb DEFAULT NULL::jsonb)
 RETURNS SETOF content_calendar
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
  update public.content_calendar c
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
  returning *;
$function$
;

-- calendar_approval_digest
CREATE OR REPLACE FUNCTION public.calendar_approval_digest(p_row content_calendar)
 RETURNS text
 LANGUAGE sql
 STABLE
 SET search_path TO 'public'
AS $function$
  select md5(concat_ws(chr(31),
    coalesce(nullif(lower(btrim(p_row.account)), ''), ''),
    coalesce(nullif(lower(btrim(p_row.format)), ''), 'feed'),
    coalesce(p_row.post_date::text, ''),
    coalesce(p_row.caption, ''),
    coalesce(nullif(btrim(p_row.image_url), ''), ''),
    coalesce(nullif(btrim(to_jsonb(p_row)->>'byte_hash'), ''), ''),
    coalesce(nullif(btrim(to_jsonb(p_row)->>'source_media_asset_id'), ''), ''),
    coalesce(nullif(btrim(to_jsonb(p_row)->>'source_media_url'), ''), ''),
    -- Preserve existing IG/FB digests. GBP proof also binds provider fields,
    -- including the destination and structured offers/events. jsonb::text has
    -- canonical key ordering, independent of input JSON key order.
    case when lower(btrim(p_row.account)) = 'googlebusiness' then
      public.calendar_gbp_approval_snapshot(p_row)::text else null end
  ));
$function$
;

-- calendar_patch_caption_autonomous_clean
CREATE OR REPLACE FUNCTION public.calendar_patch_caption_autonomous_clean(p_row_id uuid, p_gym_id text, p_expected_status text, p_expected_caption text, p_clean_caption text)
 RETURNS SETOF content_calendar
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
begin
  if p_expected_status not in ('pending', 'approved')
      or p_clean_caption is null or p_clean_caption = p_expected_caption then
    return;
  end if;
  perform pg_advisory_xact_lock(hashtextextended(p_gym_id, 0));
  if not public.calendar_gym_is_autonomous(p_gym_id) then
    return;
  end if;
  return query update public.content_calendar c
    set caption = p_clean_caption, approval_kind = null, approved_by = null,
        approved_at = null, approval_digest = null
    where c.id = p_row_id and c.gym_id = p_gym_id
      and c.status = p_expected_status
      and c.caption is not distinct from p_expected_caption
      and c.variant_status = 'active' and c.published_at is null
      and c.late_post_id is null and c.publish_claim_token is null
    returning c.*;
end;
$function$
;

-- calendar_patch_caption_manual_format
CREATE OR REPLACE FUNCTION public.calendar_patch_caption_manual_format(p_row_id uuid, p_gym_id text, p_expected_status text, p_expected_caption text, p_clean_caption text)
 RETURNS SETOF content_calendar
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_gym_count integer;
  v_gym_uuid uuid;
  v_settings_count integer;
  v_autonomous boolean;
begin
  if p_row_id is null or nullif(btrim(coalesce(p_gym_id, '')), '') is null
      or p_expected_status not in ('pending', 'approved')
      or p_clean_caption is null
      or p_clean_caption is not distinct from p_expected_caption then
    return;
  end if;
  perform pg_advisory_xact_lock(hashtextextended(p_gym_id, 0));
  select count(*), min(g.id::text)::uuid into v_gym_count, v_gym_uuid
    from public.gyms g
    join public.echo_intake_tokens t on t.gym_id = g.id
   where t.echo_account_key = p_gym_id
     and lower(coalesce(g.slug, '')) not like '%archived%'
     and lower(coalesce(g.slug, '')) not like '%-dup%'
     and lower(coalesce(g.name, '')) not like '%archived%'
     and lower(coalesce(g.name, '')) not like '%do not use%';
  if v_gym_count is distinct from 1 then return; end if;
  perform 1 from public.echo_intake_tokens t
   where t.gym_id = v_gym_uuid and t.echo_account_key = p_gym_id
   for share;
  if not found then return; end if;
  select count(*) into v_settings_count from public.echo_gym_settings s
   where s.gym_id = v_gym_uuid;
  if v_settings_count is distinct from 1 then return; end if;
  select s.autonomous into v_autonomous from public.echo_gym_settings s
   where s.gym_id = v_gym_uuid for share;
  if v_autonomous is distinct from false then return; end if;

  -- UPDATE is the row lock and CAS. Any claim, approval or caption edit that
  -- wins first changes these predicates. A changed Manual approval must be
  -- reviewed again, including rows previously approved in Automatic mode.
  return query update public.content_calendar c
     set caption = p_clean_caption, status = 'pending',
         approval_kind = null, approved_by = null,
         approved_at = null, approval_digest = null
   where c.id = p_row_id and c.gym_id = p_gym_id
     and c.status = p_expected_status
     and c.caption is not distinct from p_expected_caption
     and c.variant_status = 'active' and c.published_at is null
     and c.late_post_id is null and c.publish_claim_token is null
     and c.publish_reservation_day is null
   returning c.*;
end;
$function$
;

-- calendar_recover_unproved_approval
CREATE OR REPLACE FUNCTION public.calendar_recover_unproved_approval(p_row_id uuid, p_gym_id text, p_expected jsonb)
 RETURNS SETOF content_calendar
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_row public.content_calendar%rowtype;
begin
  if p_expected is null then return; end if;
  select * into v_row from public.content_calendar c
   where c.id = p_row_id and c.gym_id = p_gym_id
   for update of c;
  if not found or v_row.status is distinct from 'approved'
      or v_row.published_at is not null or v_row.late_post_id is not null
      or v_row.variant_status is distinct from 'active'
      or v_row.approval_kind is not null or v_row.approved_by is not null
      or v_row.approved_at is not null
      or v_row.media_not_ready_reason is not null
      or nullif(btrim(coalesce(v_row.image_url, '')), '') is null
      or (v_row.approval_digest is not null and
          v_row.approval_digest is distinct from public.calendar_approval_digest(v_row))
      or v_row.caption is distinct from p_expected->>'caption'
      or v_row.image_url is distinct from p_expected->>'media_url'
      or v_row.post_date::text is distinct from p_expected->>'day_key'
      or coalesce(nullif(lower(btrim(v_row.format)), ''), 'feed')
         is distinct from coalesce(nullif(lower(btrim(coalesce(p_expected->>'format', ''))), ''), 'feed')
      or coalesce(nullif(lower(btrim(v_row.account)), ''), '')
         is distinct from lower(btrim(coalesce(p_expected->>'platform', '')))
      or (lower(btrim(v_row.account)) = 'googlebusiness' and
          public.calendar_gbp_approval_snapshot(v_row)
            is distinct from p_expected->'gbp_proof') then
    return;
  end if;
  if v_row.approval_digest is null then
    return query
      update public.content_calendar c
         set approval_digest = public.calendar_approval_digest(c)
       where c.id = p_row_id
      returning c.*;
  else
    return next v_row;
  end if;
end;
$function$
;

-- calendar_stamp_verified_approval
CREATE OR REPLACE FUNCTION public.calendar_stamp_verified_approval(p_gym_id uuid, p_calendar_id uuid, p_clerk_actor_id text, p_echo_approval_digest text)
 RETURNS TABLE(id uuid, gym_id uuid, clerk_actor_id text, approval_digest text)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
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
    update public.content_calendar c
       set approval_kind = 'human',
           approved_by = v_actor,
           approved_at = now()
     where c.id = p_calendar_id
    returning c.id, p_gym_id, v_actor, c.approval_digest;
end;
$function$
;

-- claim_calendar_gbp_publish_owned
CREATE OR REPLACE FUNCTION public.claim_calendar_gbp_publish_owned(p_row_id uuid, p_gym_id text)
 RETURNS SETOF content_calendar
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
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
  return query update public.content_calendar c
    set status = 'publishing', publish_claim_token = gen_random_uuid()
    where c.id = p_row_id returning c.*;
end;
$function$
;

-- claim_calendar_gbp_publish_with_mode_owned
CREATE OR REPLACE FUNCTION public.claim_calendar_gbp_publish_with_mode_owned(p_row_id uuid, p_gym_id text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_row public.content_calendar%rowtype;
begin
  select * into v_row from public.claim_calendar_gbp_publish_owned(
    p_row_id, p_gym_id);
  if not found then
    return null;
  end if;
  return jsonb_build_object(
    'row', to_jsonb(v_row),
    'autonomous_at_claim', public.calendar_gym_is_autonomous(p_gym_id));
end;
$function$
;

-- claim_calendar_publish_slot
CREATE OR REPLACE FUNCTION public.claim_calendar_publish_slot(p_row_id uuid, p_gym_id text, p_day date, p_timezone text, p_capacity integer, p_approved_only boolean)
 RETURNS boolean
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_row public.content_calendar%rowtype;
  v_used integer;
begin
  if p_capacity not between 1 and 2 or p_day is null or p_timezone is null then
    return false;
  end if;
  -- Lock across day boundaries too: a previous-day in-flight claim must be
  -- visible to a worker reserving the next local day at midnight.
  perform pg_advisory_xact_lock(hashtextextended(p_gym_id, 0));
  select * into v_row from public.content_calendar
    where id = p_row_id and gym_id = p_gym_id
      and status in ('pending', 'approved') and published_at is null
      and late_post_id is null
      and variant_status = 'active'
    for update;
  if not found or (p_approved_only and v_row.status <> 'approved') then
    return false;
  end if;

  select count(*) into v_used from public.content_calendar
    where gym_id = p_gym_id
      and lower(btrim(coalesce(account, ''))) =
          lower(btrim(coalesce(v_row.account, '')))
      and coalesce(nullif(lower(btrim(format)), ''), 'feed') =
          coalesce(nullif(lower(btrim(v_row.format)), ''), 'feed')
      and (status = 'publishing' -- legacy and previous-day in-flight claims block
           or (status = 'published' and
               (publish_reservation_day = p_day
                or (published_at is not null and
                    (published_at at time zone p_timezone)::date = p_day))));
  if v_used >= p_capacity then
    return false;
  end if;
  update public.content_calendar
    set status = 'publishing', publish_reservation_day = p_day
    where id = p_row_id;
  return true;
end;
$function$
;

-- claim_calendar_publish_slot_owned
CREATE OR REPLACE FUNCTION public.claim_calendar_publish_slot_owned(p_row_id uuid, p_gym_id text, p_day date, p_timezone text, p_capacity integer, p_approved_only boolean, p_require_approval_proof boolean DEFAULT false)
 RETURNS uuid
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
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
    where id = p_row_id;
  return v_token;
end;
$function$
;

-- claim_calendar_publish_slot_proven_owned
CREATE OR REPLACE FUNCTION public.claim_calendar_publish_slot_proven_owned(p_row_id uuid, p_gym_id text, p_day date, p_timezone text, p_capacity integer, p_approved_only boolean)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_token uuid;
begin
  v_token := public.claim_calendar_publish_slot_owned(
    p_row_id, p_gym_id, p_day, p_timezone, p_capacity,
    p_approved_only, true);
  if v_token is null then
    return null;
  end if;
  return (select jsonb_build_object(
      'row', to_jsonb(c),
      'autonomous_at_claim', public.calendar_gym_is_autonomous(p_gym_id))
    from public.content_calendar c
    where c.id = p_row_id and c.gym_id = p_gym_id
      and c.status = 'publishing' and c.publish_claim_token = v_token);
end;
$function$
;

-- claim_gym_media_sync
CREATE OR REPLACE FUNCTION public.claim_gym_media_sync()
 RETURNS SETOF media_source
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
BEGIN
  -- Deliberately never steal an indexing claim by age. A long-running worker
  -- may still be writing assets; reclaiming it would create concurrent writers.
  -- After a confirmed worker crash, an operator must inspect and requeue that
  -- specific source (sync_status='queued', sync_claim_token=NULL) manually.
  RETURN QUERY WITH pick AS (
    SELECT id FROM media_source
    WHERE active AND kind = 'gym_drive' AND sync_requested_at IS NOT NULL
      AND sync_status = 'queued'
    ORDER BY sync_requested_at LIMIT 1 FOR UPDATE SKIP LOCKED
  ) UPDATE media_source s SET sync_status = 'indexing', sync_started_at = clock_timestamp(),
      sync_claim_token = md5(random()::text || clock_timestamp()::text), sync_error = NULL
    FROM pick WHERE s.id = pick.id RETURNING s.*;
END $function$
;

-- content_calendar_swap_variant
CREATE OR REPLACE FUNCTION public.content_calendar_swap_variant(p_gym_id text, p_candidate_id uuid, p_actor text DEFAULT NULL::text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_candidate   public.content_calendar%rowtype;
  v_anchor      uuid;
  v_active      public.content_calendar%rowtype;
  v_active_found boolean := false;
begin
  -- Lock the candidate row first, scoped by gym. A missing row or a
  -- cross-gym id both come back as the SAME 'not_found' -- never reveals
  -- that a row exists under a different tenant.
  select * into v_candidate
    from public.content_calendar
   where id = p_candidate_id
     and gym_id = p_gym_id
   for update;

  if not found then
    return jsonb_build_object('ok', false, 'error', 'not_found');
  end if;

  if v_candidate.variant_status <> 'candidate' then
    return jsonb_build_object('ok', false, 'error', 'not_a_candidate',
      'variant_status', v_candidate.variant_status);
  end if;

  if v_candidate.status = 'published' then
    return jsonb_build_object('ok', false, 'error', 'published_final');
  end if;

  v_anchor := coalesce(v_candidate.variant_of, v_candidate.id);

  -- Lock the WHOLE group (anchor row itself, plus every row whose variant_of
  -- points at the anchor), gym-scoped, so nothing in it can change under us
  -- for the rest of this transaction.
  perform 1
    from public.content_calendar
   where gym_id = p_gym_id
     and (id = v_anchor or variant_of = v_anchor)
   for update;

  -- Find the group's current active row (there is at most one, by the
  -- unique index; there may be zero only if the group was left in a bad
  -- state by something outside this function, which we treat as "no prior
  -- active to archive" rather than erroring).
  select * into v_active
    from public.content_calendar
   where gym_id = p_gym_id
     and (id = v_anchor or variant_of = v_anchor)
     and variant_status = 'active'
   limit 1;
  v_active_found := found;

  if v_active_found and v_active.status = 'published' then
    return jsonb_build_object('ok', false, 'error', 'published_final');
  end if;

  if v_active_found and v_active.id = v_candidate.id then
    -- Nothing to do; already active (should be unreachable given the
    -- variant_status='candidate' check above, kept as a defensive no-op).
    return jsonb_build_object('ok', true, 'noop', true,
      'active_id', v_active.id::text);
  end if;

  -- Archive every OTHER candidate in the group first (including a stale
  -- 'active' left dangling from a bad prior state), THEN promote the pick.
  -- Order matters for the unique index: archive-before-activate never has
  -- two rows both 'active' at once, even momentarily within this statement
  -- sequence (each UPDATE is its own statement, checked immediately).
  update public.content_calendar
     set variant_status = 'archived'
   where gym_id = p_gym_id
     and (id = v_anchor or variant_of = v_anchor)
     and id <> v_candidate.id
     and variant_status in ('active', 'candidate');

  update public.content_calendar
     set variant_status = 'active'
   where id = v_candidate.id
     and gym_id = p_gym_id;

  return jsonb_build_object(
    'ok', true,
    'active_id', v_candidate.id::text,
    'archived_previous_active', case when v_active_found
                                      then v_active.id::text else null end,
    'group_anchor', v_anchor::text
  );
end;
$function$
;

-- create_fixer_business_ticket_with_seed
CREATE OR REPLACE FUNCTION public.create_fixer_business_ticket_with_seed(p_ticket_id uuid, p_message_id uuid, p_submission_key uuid, p_client_id text, p_check_id text, p_params jsonb)
 RETURNS TABLE(outcome text, ticket_id uuid, request_key text)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare
  v_ticket public.support_tickets%rowtype;
  v_created boolean := false;
  v_param_count integer;
  v_raw_text text;
  v_request_key text;
  v_plan jsonb;
begin
  if p_ticket_id is null or p_message_id is null or p_submission_key is null
     or p_client_id is null
     or p_client_id !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     or p_params is null or jsonb_typeof(p_params) <> 'object' then
    raise exception 'invalid FIXER business ticket arguments';
  end if;

  select count(*) into v_param_count from jsonb_object_keys(p_params);
  if p_check_id = 'media_source_active' then
    if v_param_count <> 1
       or p_params->>'folder_id' is null
       or p_params->>'folder_id' !~ '^[A-Za-z0-9_-]{3,200}$' then
      raise exception 'invalid FIXER media business target';
    end if;
    v_raw_text := 'FIXER domain event: verify and repair Echo media source ' || (p_params->>'folder_id') || '.';
  elsif p_check_id = 'calendar_row_status' then
    if v_param_count <> 2
       or p_params->>'row_id' is null
       or p_params->>'row_id' !~ '^[A-Za-z0-9_-]{6,80}$'
       or p_params->>'expected_status' is null
       or p_params->>'expected_status' !~ '^[a-z_]{2,32}$' then
      raise exception 'invalid FIXER calendar business target';
    end if;
    v_raw_text := 'FIXER domain event: verify and repair Echo calendar row ' || (p_params->>'row_id')
      || ' to status ' || (p_params->>'expected_status') || '.';
  else
    raise exception 'unsupported FIXER business check';
  end if;

  insert into public.support_tickets
    (id, product, source, client_id, reporter, raw_text, status, lane, hold_tier, submission_key)
  values
    (p_ticket_id, 'echo', 'ops_fix', p_client_id, 'echo_domain', v_raw_text,
     'new', 'hold', 'routine', p_submission_key)
  on conflict (client_id, reporter, submission_key)
    where submission_key is not null do nothing
  returning * into v_ticket;
  v_created := found;

  if not v_created then
    select * into v_ticket from public.support_tickets
      where client_id = p_client_id and reporter = 'echo_domain'
        and submission_key = p_submission_key;
    if not found then raise exception 'FIXER business ticket receipt unavailable'; end if;
  end if;

  -- The worker intentionally excludes system inbound rows from requester input,
  -- so its request key falls back to this row's durable identity.  array_to_json
  -- has the compact JSON form used by JavaScript JSON.stringify for that array.
  v_request_key := encode(
    extensions.digest(
      convert_to(
        array_to_json(array[
          v_ticket.id::text,
          (to_jsonb(v_ticket)->>'created_at'),
          v_ticket.raw_text
        ])::text,
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );
  v_plan := jsonb_build_object(
    'schema_version', 1,
    'contract_version', 'echo-business-evidence-v1',
    'ticket_id', v_ticket.id::text,
    'client_id', p_client_id,
    'request_key', v_request_key,
    'check_id', p_check_id,
    'params', p_params
  );

  if not v_created then
    if v_ticket.product <> 'echo' or v_ticket.source <> 'ops_fix'
       or v_ticket.client_id is distinct from p_client_id
       or v_ticket.reporter <> 'echo_domain'
       or v_ticket.raw_text <> v_raw_text
       or v_ticket.verification_before is null
       or v_ticket.verification_before->'fixer'->'business_check' is distinct from v_plan then
      return query select 'payload_conflict'::text, v_ticket.id, null::text;
      return;
    end if;
  else
    update public.support_tickets as t
       set verification_before = jsonb_build_object(
         'fixer', jsonb_build_object('business_check', v_plan))
     where t.id = v_ticket.id
       and t.product = 'echo'
       and t.source = 'ops_fix'
       and t.client_id = p_client_id
       and t.status = 'new'
       and t.verification_before is null
    returning t.* into v_ticket;
    if not found then raise exception 'FIXER business ticket plan persistence unavailable'; end if;
  end if;

  -- This is an internal, non-deliverable audit record. It cannot create a
  -- reply event because only client/coach inbound messages are enqueued.
  if v_created or v_ticket.id = p_ticket_id then
    insert into public.support_messages
      (id, ticket_id, author_type, author_id, direction, body, created_at)
    values
      (p_message_id, v_ticket.id, 'system', 'echo_domain', 'inbound',
       v_ticket.raw_text, v_ticket.created_at)
    on conflict (id) do nothing;

    if not exists (
      select 1 from public.support_messages m
       where m.id = p_message_id and m.ticket_id = v_ticket.id
         and m.author_type = 'system' and m.author_id = 'echo_domain'
         and m.direction = 'inbound' and m.body = v_ticket.raw_text
    ) then
      raise exception 'FIXER business ticket system inbound missing or conflicted';
    end if;
  elsif not exists (
    select 1 from public.support_messages m
     where m.ticket_id = v_ticket.id and m.author_type = 'system'
       and m.author_id = 'echo_domain' and m.direction = 'inbound'
       and m.body = v_ticket.raw_text
       and m.created_at = v_ticket.created_at
  ) then
    raise exception 'FIXER business ticket system inbound missing after receipt conflict';
  end if;

  return query select case when v_created then 'created' else 'existing' end::text,
                      v_ticket.id, v_request_key;
end;
$function$
;

-- finish_gym_media_sync
CREATE OR REPLACE FUNCTION public.finish_gym_media_sync(p_source_id text, p_token text, p_ok boolean, p_error text DEFAULT NULL::text)
 RETURNS boolean
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE changed integer;
BEGIN
  UPDATE media_source SET
    sync_status = CASE WHEN sync_requested_at > sync_started_at THEN 'queued'
                       WHEN p_ok THEN 'ready' ELSE 'failed' END,
    sync_finished_at = clock_timestamp(),
    sync_error = CASE WHEN p_ok THEN NULL ELSE left(coalesce(p_error, 'sync failed'), 120) END,
    sync_claim_token = NULL
  WHERE id = p_source_id AND sync_claim_token = p_token AND sync_status = 'indexing';
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed > 0;
END $function$
;

-- fixer_cas_staff_swap_pointer
CREATE OR REPLACE FUNCTION public.fixer_cas_staff_swap_pointer(p_ticket_id uuid, p_expected_request_version bigint, p_expected_status text, p_expected_slack_user_id text, p_expected_slack_channel_id text, p_expected_slack_thread_ts text, p_expected_gym_id text, p_expected_echo_account_key text, p_target_row_id uuid, p_expected_image_sha text, p_expected_caption_sha text, p_state jsonb)
 RETURNS support_tickets
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare
  v_ticket public.support_tickets%rowtype;
  v_target public.content_calendar%rowtype;
  v_token public.echo_intake_tokens%rowtype;
  v_existing jsonb;
  v_pointer jsonb;
begin
  select * into v_ticket
    from public.support_tickets
   where id = p_ticket_id
   for update;
  if not found then return null; end if;

  -- The ticket identity and the exact held snapshot are caller assertions; all
  -- values are checked against rows locked/read from the database.
  if p_ticket_id <> 'ae8c7e39-7509-4948-b06a-a24954c3b0a3'::uuid
     or p_expected_gym_id <> 'e5c9db81-110d-4308-9bb7-3ad3bf563a0b'
     or p_expected_echo_account_key <> 'swiftrivercrossfite5c9db'
     or p_expected_request_version is null or p_expected_request_version < 0
     or p_expected_status is distinct from 'hold'
     or v_ticket.product is distinct from 'echo'
     or v_ticket.source is distinct from 'slack_conversation'
     or v_ticket.status is distinct from 'hold'
     or v_ticket.request_version is distinct from p_expected_request_version
     or v_ticket.identity_kind is distinct from 'coach'
     or v_ticket.client_id is not null
     or v_ticket.slack_user_id is distinct from p_expected_slack_user_id
     or v_ticket.slack_channel_id is distinct from p_expected_slack_channel_id
     or v_ticket.slack_thread_ts is distinct from p_expected_slack_thread_ts
     or coalesce(p_expected_slack_user_id,'') <> 'U06F8BUH7CG'
     or coalesce(p_expected_slack_channel_id,'') !~ '^C[A-Z0-9]{6,}$'
     or coalesce(p_expected_slack_thread_ts,'') !~ '^[0-9]+[.][0-9]+$'
     or coalesce(p_expected_gym_id,'') = ''
     or coalesce(p_expected_echo_account_key,'') = ''
     or coalesce(p_expected_image_sha,'') !~ '^[0-9a-f]{64}$'
     or coalesce(p_expected_caption_sha,'') !~ '^[0-9a-f]{64}$'
     or p_state is null or jsonb_typeof(p_state) <> 'object'
     or p_state ?| array['contract','ticket_id','request_version','slack_user_id','slack_channel_id','slack_thread_ts','gym_id','target_row_id','preimage_sha256']
     or p_state->>'gym_key' is distinct from p_expected_echo_account_key
     or p_state->>'row_id' is distinct from p_target_row_id::text
     or p_state->>'before_image_sha256' is distinct from p_expected_image_sha
     or p_state->>'caption_sha256' is distinct from p_expected_caption_sha
     or (select count(*) from jsonb_object_keys(p_state)) <> 4 then
    return null;
  end if;

  select * into v_token from public.echo_intake_tokens
   where gym_id::text = p_expected_gym_id
     and echo_account_key = p_expected_echo_account_key
   for share;
  if not found or v_token.echo_account_key is null
     or (select count(*) from public.echo_intake_tokens
         where gym_id::text = p_expected_gym_id
            or echo_account_key = p_expected_echo_account_key) <> 1 then
    return null;
  end if;

  select * into v_target from public.content_calendar
   where id = p_target_row_id and gym_id = p_expected_echo_account_key
   for share;
  if not found or v_target.status is distinct from 'pending'
     or (v_target.variant_status is not null and v_target.variant_status is distinct from 'active')
     or encode(extensions.digest(coalesce(v_target.image_url,''), 'sha256'), 'hex') is distinct from p_expected_image_sha
     or encode(extensions.digest(coalesce(v_target.caption,''), 'sha256'), 'hex') is distinct from p_expected_caption_sha then
    return null;
  end if;

  v_existing := v_ticket.verification_before->'fixer'->'staff_swap';
  if v_existing is not null then
    if v_existing->>'gym_key' is distinct from p_expected_echo_account_key
       or v_existing->>'row_id' is distinct from p_target_row_id::text
       or v_existing->>'before_image_sha256' is distinct from p_expected_image_sha
       or v_existing->>'caption_sha256' is distinct from p_expected_caption_sha then return null; end if;
    return v_ticket;
  end if;

  v_pointer := jsonb_build_object(
    'gym_key', p_expected_echo_account_key,
    'row_id', p_target_row_id::text,
    'reservation_key', extensions.gen_random_uuid()::text,
    'before_image_sha256', p_expected_image_sha,
    'caption_sha256', p_expected_caption_sha);

  update public.support_tickets as t
     set verification_before = coalesce(t.verification_before, '{}'::jsonb)
       || jsonb_build_object('fixer', coalesce(t.verification_before->'fixer', '{}'::jsonb)
         || jsonb_build_object('staff_swap', v_pointer))
   where t.id = p_ticket_id and t.status = 'hold'
     and t.request_version = p_expected_request_version
     and (t.verification_before is null
          or t.verification_before->'fixer'->'staff_swap' is null)
   returning t.* into v_ticket;
  return v_ticket;
end;
$function$
;

-- lasso_paired_story_ready_for_feed
CREATE OR REPLACE FUNCTION public.lasso_paired_story_ready_for_feed(p_feed_id uuid)
 RETURNS boolean
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  f public.content_calendar%rowtype;
  s public.content_calendar%rowtype;
begin
  select * into f from public.content_calendar where id = p_feed_id
    and gym_id = 'lasso'
    and lower(btrim(coalesce(format, 'feed'))) = 'feed'
    and coalesce(variant_status, 'active') = 'active';
  if not found or f.post_date < date '2026-10-02'
     or lower(btrim(coalesce(f.account, ''))) not in ('instagram','facebook')
     or f.slot_index not in (0,1,2)
     -- A leased feed ('publishing') is readable by the publisher's post-claim
     -- source revalidation ONLY while the lease is real: an owned claim token
     -- and reservation day, and no publish receipt yet. Any other non-pending
     -- status (including a bare 'publishing' with no lease) stays excluded.
     or (f.status not in ('pending','approved')
         and not (f.status = 'publishing'
                  and f.publish_claim_token is not null
                  and f.publish_reservation_day is not null
                  and f.published_at is null
                  and f.late_post_id is null))
     or f.media_not_ready_reason is not null
     or nullif(btrim(coalesce(f.caption, '')), '') is null
     or f.image_url !~ '^https://' then
    return false;
  end if;
  if (
    select count(*) from public.content_calendar c
     where c.gym_id = 'lasso' and c.post_date = f.post_date
       and lower(btrim(coalesce(c.account, ''))) = lower(btrim(f.account))
       and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
       and coalesce(c.variant_status, 'active') = 'active'
       and coalesce(c.status, 'pending') not in ('denied','killed','failed')
       and (c.slot_index = f.slot_index or c.slot_index is null)
       and (c.status <> 'published' or c.slot_index is null
            or not public.lasso_unrelated_published_story(c.id, f.id))
  ) <> 1 then return false; end if;
  select * into s from public.content_calendar c
   where c.gym_id = 'lasso' and c.post_date = f.post_date
     and lower(btrim(coalesce(c.account, ''))) = lower(btrim(f.account))
     and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
     and coalesce(c.variant_status, 'active') = 'active'
     and coalesce(c.status, 'pending') not in ('denied','killed','failed')
     and c.slot_index = f.slot_index
     and (c.status <> 'published'
          or not public.lasso_unrelated_published_story(c.id, f.id));
  if not found or s.status not in ('pending','approved','published')
     or s.media_not_ready_reason is distinct from null
        and s.media_not_ready_reason <> 'paired_feed_not_ready'
     or (s.status = 'published' and
         (s.published_at is null or s.late_post_id is null))
     or (s.status in ('pending','approved') and
         (s.published_at is not null or s.late_post_id is not null))
     or s.pillar is distinct from f.pillar
     or s.caption is distinct from ''
     or s.logical_post_id is distinct from f.logical_post_id
     or s.image_url !~ '^https://'
     or s.image_url = f.image_url
     or s.source_media_url is distinct from s.image_url
     or s.scheduled_at is null
     or (s.scheduled_at at time zone 'America/New_York')::date <> f.post_date
     or (f.scheduled_at is not null and
         s.scheduled_at <> f.scheduled_at + interval '15 minutes') then
    return false;
  end if;
  if exists (select 1 from public.lasso_managed_paired_stories m
             where m.story_id = s.id and m.feed_id = f.id) then
    return public.lasso_story_current_source(s.id);
  end if;
  if exists (select 1 from public.lasso_managed_paired_stories m
             where m.story_id = s.id) then
    return false;
  end if;
  -- The only unregistered exception is the existing, reviewed third-slot
  -- Summit Story path. It binds its own rendered 9:16 object to this exact
  -- feed UUID/image and to the same reviewed feed source hash.
  if lower(btrim(f.account)) <> 'instagram' or f.slot_index <> 2
     or lower(btrim(coalesce(f.pillar, ''))) <> 'summit' then
    return false;
  end if;
  return exists (
    select 1 from public.echo_infographic_artifacts story_art
    join public.echo_infographic_artifacts feed_art
      on feed_art.image_url = f.image_url
     and feed_art.tenant in ('lasso','lasso_ig')
     and feed_art.source_identity->>'source_hash' =
         story_art.source_identity->>'source_hash'
     and feed_art.evidence->>'grade_status' = 'PASS'
     and feed_art.evidence->>'image_sha256' = feed_art.image_sha256
    where story_art.tenant = 'lasso' and story_art.image_url = s.image_url
      and story_art.evidence->>'grade_status' = 'PASS'
      and story_art.evidence->>'image_sha256' = story_art.image_sha256
      and story_art.evidence->>'aspect' = '9:16'
      and story_art.evidence->>'pixels' = '1080x1920'
      and story_art.source_identity->>'source_feed_id' = f.id::text
      and story_art.source_identity->>'source_feed_image_url' = f.image_url
      and story_art.source_identity->>'source_hash' ~ '^[0-9a-f]{64}$'
  );
end;
$function$
;

-- lasso_story_current_source
CREATE OR REPLACE FUNCTION public.lasso_story_current_source(p_story_id uuid)
 RETURNS boolean
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  s public.content_calendar%rowtype;
  f public.content_calendar%rowtype;
begin
  select * into s from public.content_calendar
   where id = p_story_id and gym_id = 'lasso'
     and lower(btrim(coalesce(format, 'feed'))) = 'story'
     and coalesce(variant_status, 'active') = 'active';
  if not found or s.slot_index not in (0, 1, 2) then return false; end if;
  if (
    select count(*) from public.content_calendar c
     where c.gym_id = 'lasso' and c.post_date = s.post_date
       and lower(btrim(coalesce(c.account, ''))) =
           lower(btrim(coalesce(s.account, '')))
       and lower(btrim(coalesce(c.format, 'feed'))) = 'feed'
       and c.slot_index = s.slot_index
       and coalesce(c.variant_status, 'active') = 'active'
  ) <> 1 then return false; end if;
  select * into f from public.content_calendar c
   where c.gym_id = 'lasso' and c.post_date = s.post_date
     and lower(btrim(coalesce(c.account, ''))) =
         lower(btrim(coalesce(s.account, '')))
     and lower(btrim(coalesce(c.format, 'feed'))) = 'feed'
     and c.slot_index = s.slot_index
     and coalesce(c.variant_status, 'active') = 'active';
  if s.logical_post_id is distinct from f.logical_post_id
     or s.pillar is distinct from f.pillar
     or s.caption is distinct from ''
     or s.image_url is null or s.source_media_url is distinct from s.image_url
     or s.scheduled_at is null
     or (s.scheduled_at at time zone 'America/New_York')::date <> s.post_date
     or (f.scheduled_at is not null and
         s.scheduled_at <> f.scheduled_at + interval '15 minutes') then
    return false;
  end if;
  return public.lasso_story_review_matches(f.id, s.image_url);
end;
$function$
;

-- lasso_story_legacy_slot_guard
CREATE OR REPLACE FUNCTION public.lasso_story_legacy_slot_guard()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_account text;
begin
  if new.gym_id <> 'lasso'
     or lower(btrim(coalesce(new.format, 'feed'))) <> 'story'
     or coalesce(new.variant_status, 'active') <> 'active'
     or coalesce(new.status, 'pending') not in ('pending', 'approved', 'publishing')
     or new.slot_index not in (0, 1, 2) and new.slot_index is not null then
    return new;
  end if;
  v_account := lower(btrim(coalesce(new.account, '')));
  if v_account not in ('instagram', 'facebook') or new.post_date is null then
    return new;
  end if;
  perform pg_advisory_xact_lock(hashtextextended(
    'lasso|' || v_account || '|' || new.post_date::text, 0));
  if exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso' and c.post_date = new.post_date
       and lower(btrim(coalesce(c.account, ''))) = v_account
       and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
       and coalesce(c.variant_status, 'active') = 'active'
       and coalesce(c.status, 'pending') in ('pending', 'approved', 'publishing')
       and c.id is distinct from new.id
       and (new.slot_index is null and c.slot_index in (0, 1, 2)
            or new.slot_index in (0, 1, 2) and c.slot_index is null)
  ) then
    raise exception 'LASSO Story legacy NULL slot conflicts with numbered Story'
      using errcode = '23505';
  end if;
  return new;
end;
$function$
;

-- lasso_story_publish_source_guard
CREATE OR REPLACE FUNCTION public.lasso_story_publish_source_guard()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  f public.content_calendar%rowtype;
begin
  if new.gym_id <> 'lasso'
     or lower(btrim(coalesce(new.format, 'feed'))) <> 'story'
     or new.post_date < date '2026-10-02'
     or new.status <> 'publishing' or old.status = 'publishing' then
    return new;
  end if;
  -- Do not take over PR293's special Summit source path or preexisting legacy
  -- Stories. They enter this guard only after an exact reviewed repair.
  if not exists (select 1 from public.lasso_managed_paired_stories m
                 where m.story_id = old.id) then
    return new;
  end if;
  if not public.lasso_story_current_source(old.id)
     or old.media_not_ready_reason is not null
     or new.media_not_ready_reason is not null then
    raise exception 'LASSO Story source proof missing or held'
      using errcode = '23514';
  end if;
  select * into f from public.content_calendar
   where gym_id = 'lasso' and post_date = old.post_date
     and lower(btrim(coalesce(account, ''))) = lower(btrim(old.account))
     and lower(btrim(coalesce(format, 'feed'))) = 'feed'
     and slot_index = old.slot_index and variant_status = 'active';
  if not found or f.status <> 'published' or f.published_at is null
     or f.late_post_id is null then
    raise exception 'LASSO paired feed has no publish receipt'
      using errcode = '23514';
  end if;
  if not exists (select 1 from public.lasso_managed_paired_stories m
                 where m.story_id = old.id and m.feed_id = f.id) then
    raise exception 'LASSO managed Story feed identity changed'
      using errcode = '23514';
  end if;
  if exists (
    select 1 from public.content_calendar c where c.id <> old.id
      and c.gym_id = 'lasso' and c.post_date = old.post_date
      and lower(btrim(coalesce(c.account, ''))) = lower(btrim(old.account))
      and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
      and c.slot_index = old.slot_index
      and coalesce(c.variant_status, 'active') = 'active'
      and c.status = 'published' and c.published_at is not null
      and c.late_post_id is not null
      and not public.lasso_unrelated_published_story(c.id, f.id)
  ) then
    raise exception 'LASSO Story already published in paired slot'
      using errcode = '23514';
  end if;
  return new;
end;
$function$
;

-- lasso_story_review_matches
CREATE OR REPLACE FUNCTION public.lasso_story_review_matches(p_feed_id uuid, p_story_url text)
 RETURNS boolean
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  f public.content_calendar%rowtype;
  v_account text;
begin
  select * into f from public.content_calendar
   where id = p_feed_id and gym_id = 'lasso'
     and lower(btrim(coalesce(format, 'feed'))) = 'feed'
     and coalesce(variant_status, 'active') = 'active';
  if not found then return false; end if;
  v_account := lower(btrim(coalesce(f.account, '')));
  if v_account not in ('instagram', 'facebook')
     or f.slot_index not in (0, 1, 2)
     or f.post_date < date '2026-10-02'
     or p_story_url is null or p_story_url = f.image_url then
    return false;
  end if;
  return exists (
    select 1 from public.echo_infographic_artifacts a
     where a.tenant in ('lasso', case when v_account = 'instagram'
                                    then 'lasso_ig' else 'lasso_fb' end)
       and a.image_url = p_story_url
       and a.image_sha256 ~ '^[0-9a-f]{64}$'
       and a.evidence->>'grade_status' = 'PASS'
       and a.evidence->>'image_sha256' = a.image_sha256
       and nullif(a.evidence->>'policy_version', '') is not null
       and a.evidence->>'aspect' = '9:16'
       and a.evidence->>'pixels' = '1080x1920'
       and a.evidence->'verified_dimensions'->>'width' = '1080'
       and a.evidence->'verified_dimensions'->>'height' = '1920'
       and a.evidence->'verified_dimensions'->>'image_sha256' = a.image_sha256
       and (
         (a.tenant = 'lasso'
          and a.source_identity->>'source_id' =
              'content_calendar:' || f.id::text || ':paired_story'
          and a.source_identity->>'source_feed_id' = f.id::text
          and a.source_identity->>'source_account' = v_account
          and a.source_identity->>'source_day' = f.post_date::text
          and a.source_identity->>'source_slot' = f.slot_index::text
          and a.source_identity->>'source_feed_caption' = f.caption
          and a.source_identity->>'source_feed_image_url' = f.image_url
          and a.source_identity->>'source_logical_post_id' =
              coalesce(f.logical_post_id::text, '')
          and a.source_identity->>'source_hash' ~ '^[0-9a-f]{64}$')
         or
         (a.tenant <> 'lasso'
          and a.source_identity->>'source_id' =
              'content_calendar:' || f.id::text || ':caption'
          and a.source_identity->>'source_hash' =
              encode(sha256(convert_to(f.caption, 'UTF8')), 'hex'))
       )
  );
end;
$function$
;

-- lasso_unrelated_published_story
CREATE OR REPLACE FUNCTION public.lasso_unrelated_published_story(p_story_id uuid, p_feed_id uuid)
 RETURNS boolean
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  s public.content_calendar%rowtype;
  f public.content_calendar%rowtype;
  v_story_pillar text;
  v_feed_pillar text;
  v_day date;
  v_slot integer;
  v_story_url text;
  v_feed_url text;
  v_feed_caption text;
  v_require_published boolean;
begin
  -- These UUID pairs were independently read from the October 2/4 incident.
  -- A future published Story needs its own source review and migration.
  -- Null url/caption columns mean the pair carries no extra evidence binding.
  select x.story_pillar, x.feed_pillar, x.day, x.slot,
         x.story_url, x.feed_url, x.feed_caption, x.require_published
    into v_story_pillar, v_feed_pillar, v_day, v_slot,
         v_story_url, v_feed_url, v_feed_caption, v_require_published
    from (values
      ('af677ffb-7834-455e-852c-b865a5155ac4'::uuid,
       '81276931-45c8-470b-b658-69a34e4b17a0'::uuid,
       'website'::text, 'platform'::text, date '2026-10-02', 1,
       null::text, null::text, null::text, false),
      ('6391d2b6-9eda-4a1d-b99f-9d6885d3e876'::uuid,
       'bbd1ae3f-ce97-4587-9fca-6ffe114e1c93'::uuid,
       'platform'::text, 'echo'::text, date '2026-10-04', 1,
       null::text, null::text, null::text, false),
      ('810b7a15-d187-4f73-bad9-3fab15954cc0'::uuid,
       '2c6e3489-d965-5abc-b11b-7105047353cf'::uuid,
       'doctrine'::text, 'doctrine'::text, date '2026-10-04', 0,
       'https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/dcad077060d7579d/2026-10-04_810b7a15.png'::text,
       'https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso/91d16553122dcf21/direct_2c6e3489.png'::text,
       'Spend more time with the people in front of you.

LASSO plans your content, creates the posts, and keeps the calendar moving. See the plan in one place.

Save this for later.'::text, true)
    ) as x(story_id, feed_id, story_pillar, feed_pillar, day, slot,
           story_url, feed_url, feed_caption, require_published)
   where x.story_id = p_story_id and x.feed_id = p_feed_id;
  if not found then return false; end if;
  select * into s from public.content_calendar where id = p_story_id;
  select * into f from public.content_calendar where id = p_feed_id;
  -- Every expected-field comparison is null-safe: erased metadata must
  -- refuse, never fall through an IF guard on a NULL predicate.
  if s.id is null or f.id is null
     or s.gym_id is distinct from 'lasso' or f.gym_id is distinct from 'lasso'
     or s.post_date is distinct from f.post_date
     or s.post_date is distinct from v_day
     or lower(btrim(coalesce(s.account, ''))) <> 'instagram'
     or lower(btrim(coalesce(f.account, ''))) <> 'instagram'
     or lower(btrim(coalesce(s.format, 'feed'))) <> 'story'
     or lower(btrim(coalesce(f.format, 'feed'))) <> 'feed'
     or s.slot_index is distinct from v_slot
     or f.slot_index is distinct from v_slot
     or s.variant_status is distinct from 'active'
     or f.variant_status is distinct from 'active'
     or s.status is distinct from 'published' or s.published_at is null
     or s.late_post_id is null
     or coalesce(f.status, '') not in ('pending','approved','published','publishing')
     or (f.status = 'published' and
         (f.published_at is null or f.late_post_id is null))
     or (coalesce(f.status, '') in ('pending','approved') and
         (f.published_at is not null or f.late_post_id is not null))
     -- The only widening over the original function: the two
     -- require_published = false pairs may be read while the feed carries a
     -- REAL owned lease (claim token AND reservation day, no receipt yet).
     -- A bare, stale or incomplete lease fails closed, and the third pair is
     -- never readable here: it remains published-with-receipt only.
     or (f.status = 'publishing' and
         (v_require_published
          or f.publish_claim_token is null
          or f.publish_reservation_day is null
          or f.published_at is not null
          or f.late_post_id is not null))
     or (v_require_published and
         (f.status is distinct from 'published'
          or f.published_at is null or f.late_post_id is null))
     or s.pillar is distinct from v_story_pillar
     or f.pillar is distinct from v_feed_pillar
     or (v_story_url is not null and s.image_url is distinct from v_story_url)
     or (v_feed_url is not null and f.image_url is distinct from v_feed_url)
     or (v_feed_caption is not null
         and f.caption is distinct from v_feed_caption)
     or s.logical_post_id is not null
     or (to_jsonb(s)->>'source_media_asset_id') is not null
     or s.caption is distinct from ''
     or coalesce(s.image_url, '') !~ '^https://'
     or s.source_media_url is distinct from s.image_url then
    return false;
  end if;
  -- A historical Story with any registered or artifact source link to this
  -- feed may already satisfy the pair. Refuse a second Story in that case.
  if exists (select 1 from public.lasso_managed_paired_stories m
             where m.story_id = s.id)
     or exists (
       select 1 from public.echo_infographic_artifacts a
        where a.image_url = s.image_url
          and (a.source_identity->>'source_feed_id' = f.id::text
               or a.source_identity->>'source_id' in (
                  'content_calendar:' || f.id::text || ':paired_story',
                  'content_calendar:' || f.id::text || ':caption')
               or a.source_identity->>'source_feed_image_url' = f.image_url)
     ) then
    return false;
  end if;
  return true;
end;
$function$
;

-- portal_action_receipt_apply
CREATE OR REPLACE FUNCTION public.portal_action_receipt_apply(p_gym_id text, p_action_id text, p_request_fingerprint text, p_prepared jsonb)
 RETURNS portal_action_receipt
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE
  v_receipt public.portal_action_receipt;
  v_manifest jsonb;
  v_members jsonb;
  v_member jsonb;
  v_logical uuid;
  v_ids uuid[];
  v_id uuid;
  v_current_ids uuid[];
  v_row public.content_calendar;
  v_media jsonb;
  v_outcomes jsonb := '[]'::jsonb;
  v_after public.content_calendar;
  v_intended jsonb;
  v_persisted public.content_calendar;
  v_n integer;
BEGIN
  -- Receipt lock first: one apply per action at a time. The new prepared
  -- payload is deliberately NOT validated before this: replay of an
  -- already-terminal receipt returns the stored result for the exact
  -- binding regardless of any (malformed or missing) new payload, and a
  -- fingerprint mismatch is reported as the immutable binding conflict,
  -- never masked by a payload-shape error.
  SELECT * INTO v_receipt FROM public.portal_action_receipt
   WHERE gym_id = p_gym_id AND action_id = p_action_id
   FOR UPDATE;  -- receipt lock first: one apply per action at a time
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no receipt for this tenant and action_id'
      USING ERRCODE = 'P0002';
  END IF;
  IF v_receipt.request_fingerprint IS DISTINCT FROM p_request_fingerprint THEN
    RAISE EXCEPTION 'request fingerprint does not match the receipt binding'
      USING ERRCODE = '23514';
  END IF;
  -- Idempotent replay: a terminal receipt is returned exactly as persisted. A
  -- lost RPC response is reconciled by this return, never by a rewrite, and a
  -- timed-out write is NEVER reclassified as failed from a row read.
  IF v_receipt.status IN ('succeeded', 'failed') THEN
    RETURN v_receipt;
  END IF;

  IF jsonb_typeof(p_prepared) IS DISTINCT FROM 'object'
     OR jsonb_typeof(p_prepared->'rows') IS DISTINCT FROM 'array'
     OR jsonb_array_length(p_prepared->'rows') = 0 THEN
    RAISE EXCEPTION 'prepared swap must name a non-empty row set'
      USING ERRCODE = '22023';
  END IF;
  IF v_receipt.status <> 'selected' OR v_receipt.selected_asset IS NULL
     OR v_receipt.member_manifest IS NULL THEN
    RAISE EXCEPTION 'receipt has no claimed selection and frozen manifest to apply'
      USING ERRCODE = '23514';
  END IF;

  v_manifest := v_receipt.member_manifest;
  v_members := v_manifest->'members';
  v_logical := (v_manifest->>'logical_post_id')::uuid;
  IF v_logical IS NULL THEN
    RAISE EXCEPTION
      'frozen manifest has no logical_post_id; conflicting request held for manual review'
      USING ERRCODE = '23514';
  END IF;

  -- The prepared row set must equal the frozen manifest row set exactly: no
  -- repick, no omission, no extra.
  SELECT array_agg((e.value->>'calendar_row_id')::uuid
                   ORDER BY (e.value->>'calendar_row_id')::uuid)
    INTO v_ids
    FROM jsonb_array_elements(p_prepared->'rows') e;
  IF v_ids IS NULL
     OR cardinality(v_ids) <> (SELECT count(DISTINCT id) FROM unnest(v_ids) a(id)) THEN
    RAISE EXCEPTION 'prepared rows must name each row exactly once'
      USING ERRCODE = '22023';
  END IF;
  IF v_ids IS DISTINCT FROM (
       SELECT array_agg((m.value->>'calendar_row_id')::uuid
                        ORDER BY (m.value->>'calendar_row_id')::uuid)
         FROM jsonb_array_elements(v_members) m) THEN
    RAISE EXCEPTION 'prepared rows must match the frozen member manifest exactly'
      USING ERRCODE = '23514';
  END IF;

  -- PHANTOM EXCLUSION (documented lock strategy): SHARE ROW EXCLUSIVE
  -- conflicts with ROW EXCLUSIVE, so no concurrent INSERT/UPDATE/DELETE on
  -- content_calendar can commit for the rest of this transaction; a newly
  -- inserted active same-logical-post sibling cannot slip past the
  -- deterministic row locks taken below.
  LOCK TABLE public.content_calendar IN SHARE ROW EXCLUSIVE MODE;

  -- Deterministic row locks, id ascending.
  FOR v_id IN SELECT id FROM unnest(v_ids) u(id) ORDER BY id LOOP
    PERFORM 1 FROM public.content_calendar WHERE id = v_id FOR UPDATE;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'manifest sibling row % is gone', v_id USING ERRCODE = '23514';
    END IF;
  END LOOP;

  -- Re-verify the EXACT CURRENT active member set against the frozen manifest.
  -- Any activation, archival, or new sibling since selection aborts the group.
  SELECT array_agg(c.id ORDER BY c.id) INTO v_current_ids
    FROM public.content_calendar c
   WHERE c.gym_id = p_gym_id
     AND c.logical_post_id = v_logical
     AND c.variant_status = 'active';
  IF v_current_ids IS DISTINCT FROM v_ids THEN
    RAISE EXCEPTION 'active logical-post membership changed since selection'
      USING ERRCODE = '23514';
  END IF;

  -- VALIDATION PHASE (no writes): every row is checked against its OWN
  -- manifest before-state (tenant, group + row identity including
  -- account/format/post_date, status, no publish markers, no media hold,
  -- unchanged caption/media) and its prepared media is checked against the
  -- frozen per-row intended identity (exact selected_asset /
  -- planned_siblings equality, own-tenant asset ownership, clean public URLs
  -- for image/source/thumbnail) BEFORE any row is mutated. A repicked,
  -- foreign, or tokenized prepared identity aborts before the first write.
  FOR v_member IN SELECT m.value FROM jsonb_array_elements(v_members) m
                  ORDER BY (m.value->>'calendar_row_id')::uuid LOOP
    v_id := (v_member->>'calendar_row_id')::uuid;
    SELECT * INTO v_row FROM public.content_calendar WHERE id = v_id;
    IF v_row.gym_id IS DISTINCT FROM p_gym_id
       OR v_row.logical_post_id IS DISTINCT FROM v_logical
       OR v_row.variant_status IS DISTINCT FROM 'active'
       OR v_row.account IS DISTINCT FROM (v_member->>'account')
       OR v_row.format IS DISTINCT FROM (v_member->>'format')
       OR v_row.post_date IS DISTINCT FROM (v_member->>'post_date')::date
       OR v_row.status NOT IN ('pending', 'coach_review')
       OR v_row.published_at IS NOT NULL OR v_row.late_post_id IS NOT NULL
       OR v_row.publish_claim_token IS NOT NULL
       OR v_row.media_not_ready_reason IS NOT NULL
       OR v_row.status IS DISTINCT FROM (v_member->>'status')
       OR coalesce(v_row.caption, '') IS DISTINCT FROM coalesce(v_member->>'caption', '')
       OR v_row.image_url IS DISTINCT FROM (v_member->>'image_url')
       OR v_row.thumbnail_url IS DISTINCT FROM (v_member->>'thumbnail_url')
       OR v_row.source_media_url IS DISTINCT FROM (v_member->>'source_media_url')
       OR v_row.source_media_asset_id IS DISTINCT FROM (v_member->>'source_media_asset_id') THEN
      RAISE EXCEPTION 'stale eligible sibling or media hold on row %', v_id
        USING ERRCODE = '23514';
    END IF;

    SELECT e.value->'media' INTO v_media
      FROM jsonb_array_elements(p_prepared->'rows') e
     WHERE (e.value->>'calendar_row_id')::uuid = v_id;
    IF v_media IS NULL OR jsonb_typeof(v_media) IS DISTINCT FROM 'object'
       OR EXISTS (SELECT 1 FROM jsonb_object_keys(v_media) k
                  WHERE k NOT IN ('image_url', 'source_media_url',
                                  'thumbnail_url', 'source_media_asset_id'))
       OR NOT public.portal_action_receipt_clean_url(v_media->>'image_url')
       OR (v_media ? 'source_media_url' AND v_media->>'source_media_url' IS NOT NULL
           AND NOT public.portal_action_receipt_clean_url(v_media->>'source_media_url'))
       OR (v_media ? 'thumbnail_url' AND v_media->>'thumbnail_url' IS NOT NULL
           AND NOT public.portal_action_receipt_clean_url(v_media->>'thumbnail_url')) THEN
      RAISE EXCEPTION 'prepared media for row % is not allowlisted', v_id
        USING ERRCODE = '22023';
    END IF;

    -- Frozen intended identity for this row: its own planned_siblings entry,
    -- else the frozen selected asset. Exact equality with NULL-tolerant
    -- optional source/thumbnail/asset keys: no repick, no foreign asset and
    -- no tokenized URL can differ from what claim froze.
    SELECT s.value INTO v_intended
      FROM jsonb_each(v_receipt.planned_siblings) s
     WHERE s.key::uuid = v_id;
    IF NOT FOUND THEN
      v_intended := jsonb_build_object(
        'image_url', v_receipt.selected_asset->>'image_url',
        'source_media_url', v_receipt.selected_asset->>'source_media_url',
        'thumbnail_url', v_receipt.selected_asset->>'thumbnail_url',
        'source_media_asset_id', v_receipt.selected_asset->>'asset_id');
    END IF;
    IF v_media->>'image_url' IS DISTINCT FROM (v_intended->>'image_url')
       OR v_media->>'source_media_url' IS DISTINCT FROM (v_intended->>'source_media_url')
       OR v_media->>'thumbnail_url' IS DISTINCT FROM (v_intended->>'thumbnail_url')
       OR v_media->>'source_media_asset_id' IS DISTINCT FROM (v_intended->>'source_media_asset_id') THEN
      RAISE EXCEPTION 'prepared media for row % does not match the frozen selection', v_id
        USING ERRCODE = '23514';
    END IF;
    -- Own-tenant asset ownership on the identity actually being written:
    -- a foreign, coach-excluded or no-longer-eligible asset id is refused
    -- before mutation ('local:<key>' is the local-library provenance the
    -- claim allowlisted; it has no media_asset row by design). The asset is
    -- re-checked HERE, at apply, so an eligibility/exclusion flip between
    -- claim and apply aborts the group -- and the row is locked FOR SHARE
    -- THROUGH the calendar writes below, so a concurrent coach flip either
    -- commits first (this recheck then refuses) or waits out this whole
    -- transaction (serialized); it can never slip between the recheck and
    -- the calendar updates. FOR SHARE, not FOR UPDATE: apply never writes
    -- media_asset, and the weaker mode avoids a needless write lock while
    -- still conflicting with any concurrent UPDATE row lock.
    IF v_media->>'source_media_asset_id' IS NOT NULL
       AND v_media->>'source_media_asset_id' NOT LIKE 'local:%' THEN
      PERFORM 1 FROM public.media_asset
       WHERE id = v_media->>'source_media_asset_id' AND gym_id = p_gym_id
         AND eligible IS TRUE
         AND excluded_by_coach IS NOT TRUE
       FOR SHARE;
      IF NOT FOUND THEN
        RAISE EXCEPTION 'prepared media for row % is not an own-tenant allowlisted media asset', v_id
          USING ERRCODE = '23514';
      END IF;
    END IF;
  END LOOP;

  -- WRITE PHASE: full expected-before CAS UPDATE per row in deterministic id
  -- order (account/format/post_date are part of the CAS: the manifest froze
  -- them and a re-dated or re-targeted row is stale). Zero matched rows means
  -- a concurrent change won and the whole transaction aborts; approved,
  -- publishing/published, and media-held rows are never mutated. After EVERY
  -- update the persisted row is re-read and must show the expected media, an
  -- unchanged eligible status, active group membership and no media
  -- hold/claim -- a soft BEFORE UPDATE trigger that altered NEW still leaves
  -- ROW_COUNT = 1, so only the persisted re-read catches that drift; any
  -- drift raises and rolls the whole transaction back.
  FOR v_member IN SELECT m.value FROM jsonb_array_elements(v_members) m
                  ORDER BY (m.value->>'calendar_row_id')::uuid LOOP
    v_id := (v_member->>'calendar_row_id')::uuid;
    SELECT * INTO v_row FROM public.content_calendar WHERE id = v_id;
    SELECT e.value->'media' INTO v_media
      FROM jsonb_array_elements(p_prepared->'rows') e
     WHERE (e.value->>'calendar_row_id')::uuid = v_id;

    UPDATE public.content_calendar
       SET image_url = v_media->>'image_url',
           source_media_url = v_media->>'source_media_url',
           thumbnail_url = v_media->>'thumbnail_url',
           source_media_asset_id = v_media->>'source_media_asset_id',
           media_not_ready_reason = NULL
     WHERE id = v_id AND gym_id = p_gym_id
       AND logical_post_id = v_logical
       AND variant_status = 'active'
       AND account IS NOT DISTINCT FROM (v_member->>'account')
       AND format IS NOT DISTINCT FROM (v_member->>'format')
       AND post_date IS NOT DISTINCT FROM (v_member->>'post_date')::date
       AND status IN ('pending', 'coach_review')
       AND published_at IS NULL AND late_post_id IS NULL
       AND publish_claim_token IS NULL
       AND media_not_ready_reason IS NULL
       AND caption IS NOT DISTINCT FROM v_row.caption
       AND image_url IS NOT DISTINCT FROM (v_member->>'image_url')
       AND thumbnail_url IS NOT DISTINCT FROM (v_member->>'thumbnail_url')
       AND source_media_url IS NOT DISTINCT FROM (v_member->>'source_media_url')
       AND source_media_asset_id IS NOT DISTINCT FROM (v_member->>'source_media_asset_id');
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n <> 1 THEN
      RAISE EXCEPTION 'stale sibling row % aborted the swap group', v_id
        USING ERRCODE = '23514';
    END IF;

    -- Persisted re-read: ROW_COUNT alone cannot see a soft trigger rewrite.
    SELECT * INTO v_persisted FROM public.content_calendar WHERE id = v_id;
    IF NOT FOUND
       OR v_persisted.gym_id IS DISTINCT FROM p_gym_id
       OR v_persisted.logical_post_id IS DISTINCT FROM v_logical
       OR v_persisted.variant_status IS DISTINCT FROM 'active'
       OR v_persisted.account IS DISTINCT FROM v_row.account
       OR v_persisted.format IS DISTINCT FROM v_row.format
       OR v_persisted.post_date IS DISTINCT FROM v_row.post_date
       OR v_persisted.status IS DISTINCT FROM v_row.status
       OR v_persisted.status NOT IN ('pending', 'coach_review')
       OR v_persisted.published_at IS NOT NULL
       OR v_persisted.late_post_id IS NOT NULL
       OR v_persisted.publish_claim_token IS NOT NULL
       OR v_persisted.media_not_ready_reason IS NOT NULL
       OR v_persisted.image_url IS DISTINCT FROM (v_media->>'image_url')
       OR v_persisted.source_media_url IS DISTINCT FROM (v_media->>'source_media_url')
       OR v_persisted.thumbnail_url IS DISTINCT FROM (v_media->>'thumbnail_url')
       OR v_persisted.source_media_asset_id IS DISTINCT FROM (v_media->>'source_media_asset_id') THEN
      RAISE EXCEPTION 'post-write drift on row % aborted the swap group', v_id
        USING ERRCODE = '23514';
    END IF;

    v_outcomes := v_outcomes || jsonb_build_array(jsonb_build_object(
      'id', v_id::text, 'swapped', true,
      'image_url', v_media->>'image_url',
      'source_media_asset_id', v_media->>'source_media_asset_id'));
  END LOOP;

  SELECT * INTO v_after FROM public.content_calendar WHERE id = v_receipt.row_id;

  -- Terminal success is persisted in the SAME transaction as the media writes;
  -- only this persisted receipt is ever returned as success.
  UPDATE public.portal_action_receipt
     SET status = 'succeeded',
         after_state = jsonb_build_object(
           'id', v_after.id::text, 'status', v_after.status,
           'caption', v_after.caption, 'post_date', v_after.post_date::text,
           'format', v_after.format, 'image_url', v_after.image_url,
           'thumbnail_url', v_after.thumbnail_url,
           'source_media_url', v_after.source_media_url,
           'source_media_asset_id', v_after.source_media_asset_id),
         sibling_outcomes = v_outcomes,
         response_status = 200,
         error = NULL
   WHERE id = v_receipt.id AND status = 'selected'
  RETURNING * INTO v_receipt;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'receipt terminal write raced; aborting the group'
      USING ERRCODE = '23514';
  END IF;
  RETURN v_receipt;
END;
$function$
;

-- portal_action_receipt_begin
CREATE OR REPLACE FUNCTION public.portal_action_receipt_begin(p_gym_id text, p_action_id text, p_action text, p_row_id uuid, p_actor_id text, p_request_fingerprint text)
 RETURNS portal_action_receipt
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE
  v_receipt public.portal_action_receipt;
  v_row public.content_calendar;
  v_logical uuid;
  v_before jsonb;
BEGIN
  IF nullif(btrim(p_gym_id), '') IS NULL
     OR nullif(btrim(p_action_id), '') IS NULL OR length(p_action_id) > 128
     OR nullif(btrim(p_action), '') IS NULL
     OR p_row_id IS NULL
     OR p_request_fingerprint IS NULL
     OR p_request_fingerprint !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid receipt begin arguments' USING ERRCODE = '22023';
  END IF;

  -- Existing receipt FIRST, before any calendar lookup: replay /
  -- conflicting-reuse is resolved from the immutable binding alone, so a
  -- replay aimed at a since-removed, nonexistent or historical-NULL row
  -- still returns the stored binding (exact replay) or raises the immutable
  -- action conflict -- never a manual-review or missing-row error. SELECT
  -- FOR UPDATE also waits out a concurrent in-flight INSERT of the same key.
  SELECT * INTO v_receipt FROM public.portal_action_receipt
   WHERE gym_id = p_gym_id AND action_id = p_action_id
   FOR UPDATE;
  IF FOUND THEN
    IF v_receipt.action IS DISTINCT FROM p_action
       OR v_receipt.row_id IS DISTINCT FROM p_row_id
       OR v_receipt.actor_id IS DISTINCT FROM coalesce(p_actor_id, '')
       OR v_receipt.request_fingerprint IS DISTINCT FROM p_request_fingerprint THEN
      RAISE EXCEPTION 'conflicting reuse of action_id with a different request binding'
        USING ERRCODE = '23514';
    END IF;
    RETURN v_receipt;
  END IF;

  -- Genuinely new action: SQL begin is authoritative for the first
  -- before_state. No caller-supplied before image is ever accepted; the
  -- own-tenant primary row is read HERE, in the same transaction that
  -- inserts the receipt, and the persisted snapshot is captured from it.
  -- The primary must carry a non-null logical_post_id; a historical NULL
  -- (unbackfilled) or missing row is HELD: no receipt is inserted and the
  -- action is routed to manual review. Sibling grouping by date/media
  -- inference is never a fallback.
  SELECT c.* INTO v_row
    FROM public.content_calendar c
   WHERE c.id = p_row_id AND c.gym_id = p_gym_id;
  v_logical := v_row.logical_post_id;
  IF v_logical IS NULL THEN
    RAISE EXCEPTION
      'primary row has no logical_post_id; conflicting request held for manual review'
      USING ERRCODE = '23514';
  END IF;
  v_before := jsonb_build_object(
    'id', v_row.id::text, 'status', v_row.status,
    'caption', v_row.caption, 'post_date', v_row.post_date::text,
    'format', v_row.format, 'image_url', v_row.image_url,
    'thumbnail_url', v_row.thumbnail_url,
    'source_media_url', v_row.source_media_url,
    'source_media_asset_id', v_row.source_media_asset_id);

  -- Race-safe insert: a concurrent begin of the same key loses ON CONFLICT
  -- DO NOTHING and falls through to the locked binding re-check below.
  INSERT INTO public.portal_action_receipt
    (gym_id, action_id, action, row_id, actor_id, request_fingerprint,
     status, before_state)
  VALUES
    (p_gym_id, p_action_id, p_action, p_row_id, coalesce(p_actor_id, ''),
     p_request_fingerprint, 'started', v_before)
  ON CONFLICT (gym_id, action_id) DO NOTHING
  RETURNING * INTO v_receipt;
  IF FOUND THEN
    RETURN v_receipt;
  END IF;

  -- Lost the insert race: wait out the winner's INSERT and apply the same
  -- immutable binding check, so a duplicate begin can never observe its own
  -- action as missing and a racing conflicting binding still raises.
  SELECT * INTO v_receipt FROM public.portal_action_receipt
   WHERE gym_id = p_gym_id AND action_id = p_action_id
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'receipt insert conflicted but no receipt is visible'
      USING ERRCODE = 'P0002';
  END IF;
  IF v_receipt.action IS DISTINCT FROM p_action
     OR v_receipt.row_id IS DISTINCT FROM p_row_id
     OR v_receipt.actor_id IS DISTINCT FROM coalesce(p_actor_id, '')
     OR v_receipt.request_fingerprint IS DISTINCT FROM p_request_fingerprint THEN
    RAISE EXCEPTION 'conflicting reuse of action_id with a different request binding'
      USING ERRCODE = '23514';
  END IF;
  RETURN v_receipt;
END;
$function$
;

-- portal_action_receipt_claim_selection
CREATE OR REPLACE FUNCTION public.portal_action_receipt_claim_selection(p_gym_id text, p_action_id text, p_request_fingerprint text, p_selected_asset jsonb, p_planned_siblings jsonb)
 RETURNS portal_action_receipt
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE
  v_receipt public.portal_action_receipt;
  v_asset jsonb;
  v_asset_row public.media_asset;
  v_origin text;
  v_base text;
  v_slug text;
  v_siblings jsonb := '{}'::jsonb;
  v_entry record;
  v_media jsonb;
  v_row_gym text;
  v_key text;
  v_logical uuid;
  v_members jsonb;
  v_manifest jsonb;
BEGIN
  SELECT * INTO v_receipt FROM public.portal_action_receipt
   WHERE gym_id = p_gym_id AND action_id = p_action_id
   FOR UPDATE;  -- serializes concurrent claimants for the full CAS
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no receipt for this tenant and action_id'
      USING ERRCODE = 'P0002';
  END IF;
  IF v_receipt.request_fingerprint IS DISTINCT FROM p_request_fingerprint THEN
    RAISE EXCEPTION 'request fingerprint does not match the receipt binding'
      USING ERRCODE = '23514';
  END IF;
  -- Terminal or already-selected: the loser / replayer receives the stored
  -- (winner) selection and manifest. A frozen selection is never overwritten.
  IF v_receipt.status IN ('succeeded', 'failed') OR v_receipt.selected_asset IS NOT NULL THEN
    RETURN v_receipt;
  END IF;
  IF v_receipt.status NOT IN ('started', 'uncertain') THEN
    RAISE EXCEPTION 'receipt is not claimable in status %', v_receipt.status
      USING ERRCODE = '23514';
  END IF;

  -- Trusted public origin, operator-configured and NEVER caller-controlled.
  -- Fail CLOSED while unset or malformed: no URL identity is accepted until
  -- the operator's rollout seed lands.
  SELECT value INTO v_origin FROM public.portal_action_receipt_config
   WHERE key = 'public_origin';
  IF v_origin IS NULL
     OR v_origin !~ '^https://[a-z0-9.-]+(:[0-9]+)?$' THEN
    RAISE EXCEPTION 'portal action receipts have no trusted public media origin configured; refusing closed'
      USING ERRCODE = '23514';
  END IF;

  -- Candidate validation: strict key allowlist, no picker dict, no local path,
  -- no raw response. Only asset_id + public object identity keys survive.
  IF jsonb_typeof(p_selected_asset) IS DISTINCT FROM 'object'
     OR EXISTS (SELECT 1 FROM jsonb_object_keys(p_selected_asset) k
                WHERE k NOT IN ('asset_id', 'image_url', 'source_media_url',
                                'thumbnail_url', 'kind', 'key'))
     OR nullif(btrim(coalesce(p_selected_asset->>'asset_id', '')), '') IS NULL
     OR NOT public.portal_action_receipt_clean_url(p_selected_asset->>'image_url')
     OR (p_selected_asset ? 'source_media_url'
         AND p_selected_asset->>'source_media_url' IS NOT NULL
         AND NOT public.portal_action_receipt_clean_url(p_selected_asset->>'source_media_url'))
     OR (p_selected_asset ? 'thumbnail_url'
         AND p_selected_asset->>'thumbnail_url' IS NOT NULL
         AND NOT public.portal_action_receipt_clean_url(p_selected_asset->>'thumbnail_url')) THEN
    RAISE EXCEPTION 'selected asset must be an allowlisted public object identity'
      USING ERRCODE = '22023';
  END IF;
  -- Own-tenant public object allowlist. A 'local:<library-key>' id is the
  -- local-library provenance minted ONLY by the service-role swap picker from
  -- the tenant's own library root (no media_asset row exists for it); every
  -- other id must be an own-tenant, still-ELIGIBLE, not coach-excluded
  -- media_asset. The asset row is locked FOR UPDATE so an eligibility or
  -- exclusion flip cannot race this claim.
  IF p_selected_asset->>'asset_id' NOT LIKE 'local:%' THEN
    SELECT * INTO v_asset_row FROM public.media_asset
     WHERE id = p_selected_asset->>'asset_id' AND gym_id = p_gym_id
       AND eligible IS TRUE
       AND excluded_by_coach IS NOT TRUE
     FOR UPDATE;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'selected asset is not an own-tenant allowlisted media asset'
        USING ERRCODE = '23514';
    END IF;
  END IF;

  -- Hosted-URL tenant binding to the TRUSTED configured origin: every URL
  -- this pipeline mints is a tenant-scoped, content-addressed Echo object
  -- on the operator-configured public origin
  -- (media_host._build_key/_public_url: <origin>/echo/<slug(gym
  -- base)>/<sha1-16>/<file> for Drive picks,
  -- <origin>/echo/<slug(base)_ig>/<sha1-16>/<file> for local picks and
  -- story re-burns). A valid own asset id can never launder a cross-gym or
  -- foreign-host public URL. strpos (literal, position 1), not LIKE:
  -- tenant slugs may contain the LIKE wildcard '_'.
  v_base := regexp_replace(p_gym_id, '_(ig|fb|gbp)$', '');
  v_slug := lower(regexp_replace(v_base, '[^a-z0-9_-]+', '-', 'g'));
  v_slug := regexp_replace(v_slug, '-{2,}', '-', 'g');
  v_slug := btrim(v_slug, '-_');
  IF v_slug = '' THEN
    v_slug := 'tenant';
  END IF;
  IF NOT public.portal_action_receipt_hosted_url(p_selected_asset->>'image_url', v_origin, v_slug)
     OR (p_selected_asset ? 'source_media_url'
         AND p_selected_asset->>'source_media_url' IS NOT NULL
         AND NOT public.portal_action_receipt_hosted_url(p_selected_asset->>'source_media_url', v_origin, v_slug))
     OR (p_selected_asset ? 'thumbnail_url'
         AND p_selected_asset->>'thumbnail_url' IS NOT NULL
         AND NOT public.portal_action_receipt_hosted_url(p_selected_asset->>'thumbnail_url', v_origin, v_slug)) THEN
    RAISE EXCEPTION 'selected media URL is not under this tenant''s hosted media prefix'
      USING ERRCODE = '23514';
  END IF;
  v_asset := jsonb_build_object(
    'asset_id', p_selected_asset->>'asset_id',
    'image_url', p_selected_asset->>'image_url',
    'source_media_url', p_selected_asset->>'source_media_url',
    'thumbnail_url', p_selected_asset->>'thumbnail_url',
    'kind', p_selected_asset->>'kind',
    'key', p_selected_asset->>'key');

  -- Planned sibling identity: map of own-tenant calendar row uuid -> allowlisted
  -- per-row media identity. No other keys are retained.
  IF p_planned_siblings IS NOT NULL THEN
    IF jsonb_typeof(p_planned_siblings) IS DISTINCT FROM 'object' THEN
      RAISE EXCEPTION 'planned siblings must be a row-id to media identity map'
        USING ERRCODE = '22023';
    END IF;
    FOR v_entry IN SELECT key, value FROM jsonb_each(p_planned_siblings) LOOP
      v_key := v_entry.key;
      v_media := v_entry.value;
      IF v_key !~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
         OR jsonb_typeof(v_media) IS DISTINCT FROM 'object'
         OR EXISTS (SELECT 1 FROM jsonb_object_keys(v_media) k
                    WHERE k NOT IN ('image_url', 'source_media_url',
                                    'thumbnail_url', 'source_media_asset_id'))
         OR NOT public.portal_action_receipt_clean_url(v_media->>'image_url')
         OR (v_media ? 'source_media_url' AND v_media->>'source_media_url' IS NOT NULL
             AND NOT public.portal_action_receipt_clean_url(v_media->>'source_media_url'))
         OR (v_media ? 'thumbnail_url' AND v_media->>'thumbnail_url' IS NOT NULL
             AND NOT public.portal_action_receipt_clean_url(v_media->>'thumbnail_url')) THEN
        RAISE EXCEPTION 'planned sibling identity is not allowlisted public media'
          USING ERRCODE = '22023';
      END IF;
      SELECT gym_id INTO v_row_gym FROM public.content_calendar WHERE id = v_key::uuid;
      IF v_row_gym IS DISTINCT FROM p_gym_id THEN
        RAISE EXCEPTION 'planned sibling row is not owned by this tenant'
          USING ERRCODE = '23514';
      END IF;
      -- Same trusted-origin hosted-URL tenant binding as the selected
      -- asset (image and any present source/thumbnail URL).
      IF NOT public.portal_action_receipt_hosted_url(v_media->>'image_url', v_origin, v_slug)
         OR (v_media ? 'source_media_url' AND v_media->>'source_media_url' IS NOT NULL
             AND NOT public.portal_action_receipt_hosted_url(v_media->>'source_media_url', v_origin, v_slug))
         OR (v_media ? 'thumbnail_url' AND v_media->>'thumbnail_url' IS NOT NULL
             AND NOT public.portal_action_receipt_hosted_url(v_media->>'thumbnail_url', v_origin, v_slug)) THEN
        RAISE EXCEPTION 'planned sibling URL is not under this tenant''s hosted media prefix'
          USING ERRCODE = '23514';
      END IF;
      v_siblings := v_siblings || jsonb_build_object(v_key, v_media);
    END LOOP;
  END IF;

  -- Freeze the EXACT active member manifest at selection: every own-tenant row
  -- sharing the primary's logical_post_id with variant_status = 'active', each
  -- with its own identity and before-state. No date/media inference; a Story
  -- rendition with different media is included, an unrelated post with the
  -- same date and asset is not, archived/candidate variants are excluded.
  SELECT c.logical_post_id INTO v_logical
    FROM public.content_calendar c
   WHERE c.id = v_receipt.row_id AND c.gym_id = p_gym_id;
  IF v_logical IS NULL THEN
    RAISE EXCEPTION
      'primary row has no logical_post_id; conflicting request held for manual review'
      USING ERRCODE = '23514';
  END IF;
  SELECT jsonb_agg(jsonb_build_object(
           'calendar_row_id', c.id::text,
           'format', c.format,
           'account', c.account,
           'post_date', c.post_date::text,
           'status', c.status,
           'caption', c.caption,
           'image_url', c.image_url,
           'thumbnail_url', c.thumbnail_url,
           'source_media_url', c.source_media_url,
           'source_media_asset_id', c.source_media_asset_id)
           ORDER BY c.id)
    INTO v_members
    FROM public.content_calendar c
   WHERE c.gym_id = p_gym_id
     AND c.logical_post_id = v_logical
     AND c.variant_status = 'active';
  IF v_members IS NULL
     OR NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v_members) m
                    WHERE (m.value->>'calendar_row_id')::uuid = v_receipt.row_id) THEN
    RAISE EXCEPTION 'primary row is not in its own active logical-post group'
      USING ERRCODE = '23514';
  END IF;
  v_manifest := jsonb_build_object(
    'logical_post_id', v_logical::text,
    'members', v_members);

  -- The CAS itself: exclusive transition from started/unselected. The FOR
  -- UPDATE row lock above makes two concurrent claims serialize; the predicate
  -- is the backstop. The manifest freezes WITH the selection, atomically.
  UPDATE public.portal_action_receipt
     SET selected_asset = v_asset, planned_siblings = v_siblings,
         member_manifest = v_manifest,
         status = 'selected'
   WHERE id = v_receipt.id
     AND status IN ('started', 'uncertain')
     AND selected_asset IS NULL
  RETURNING * INTO v_receipt;
  IF NOT FOUND THEN
    -- Lost the race between lock and update: return the winner's selection.
    SELECT * INTO v_receipt FROM public.portal_action_receipt
     WHERE id = v_receipt.id;
  END IF;
  RETURN v_receipt;
END;
$function$
;

-- portal_swap_reserve
CREATE OR REPLACE FUNCTION public.portal_swap_reserve(p_gym_id uuid, p_post_id uuid, p_action_id uuid, p_actor_id text, p_account_key text, p_baseline_image text, p_baseline_caption text)
 RETURNS text
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  r jsonb;
begin
  -- SELECT * into jsonb so Echo-owned columns (logical_post_id,
  -- media_not_ready_reason) are read defensively: present in the shared prod
  -- table, absent nowhere the app runs, and a missing key reads as NULL here
  -- instead of erroring the function.
  execute 'select to_jsonb(t) from (select * from public.content_calendar where id = $1 for update) t'
    into r using p_post_id;

  if r is null then
    return 'not_found';
  end if;

  -- Tenant proof: the row's account key must be THIS gym's resolved Echo key.
  if (r ->> 'gym_id') is distinct from p_account_key then
    return 'cross_tenant';
  end if;

  -- Guard every row, including those with logical_post_id. Portal forwarding
  -- of action_id is allowlisted separately and does not prove Echo's receipt
  -- feature is enabled. Bypassing here would recreate the duplicate-send gap.

  -- Approved / live / publishing / media-held rows cannot swap.
  if (r ->> 'status') in ('approved', 'published', 'publishing')
     or (r ->> 'media_not_ready_reason') is not null then
    return 'not_swappable';
  end if;

  -- Stale baseline: the client is looking at an old creative or caption.
  if (r ->> 'image_url') is distinct from p_baseline_image
     or (r ->> 'caption') is distinct from p_baseline_caption then
    return 'stale_baseline';
  end if;

  -- A finished or held action ID is single-use: replay never resends.
  if exists (select 1 from public.portal_swap_guard where action_id = p_action_id) then
    return 'action_replayed';
  end if;

  begin
    insert into public.portal_swap_guard
      (gym_id, account_key, post_id, action_id, actor_id,
       baseline_image_url, baseline_caption, baseline_status, status)
    values
      (p_gym_id, p_account_key, p_post_id, p_action_id, p_actor_id,
       p_baseline_image, p_baseline_caption, r ->> 'status', 'active');
  exception
    when unique_violation then
      return 'conflict_active';
  end;

  return 'reserved';
end;
$function$
;

-- record_gym_media_review
CREATE OR REPLACE FUNCTION public.record_gym_media_review(p_gym_id text, p_asset_id text, p_expected_hash text, p_expected_status text, p_expected_reviewed_at timestamp with time zone, p_fields jsonb)
 RETURNS boolean
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE
  current_asset public.media_asset%ROWTYPE;
  new_status text;
  new_at timestamptz;
BEGIN
  IF p_gym_id IS NULL OR p_asset_id IS NULL OR
     coalesce(btrim(p_expected_hash), '') = '' OR
     jsonb_typeof(p_fields) <> 'object' THEN
    RETURN false;
  END IF;
  SELECT * INTO current_asset FROM public.media_asset
    WHERE id = p_asset_id AND gym_id = p_gym_id FOR UPDATE;
  IF NOT FOUND OR current_asset.content_hash IS DISTINCT FROM p_expected_hash OR
     current_asset.review_status IS DISTINCT FROM p_expected_status OR
     current_asset.reviewed_at IS DISTINCT FROM p_expected_reviewed_at OR
     (p_fields->>'review_content_hash') IS DISTINCT FROM p_expected_hash THEN
    RETURN false;
  END IF;
  new_status := p_fields->>'review_status';
  IF new_status IS NULL OR
     new_status NOT IN ('approved', 'rejected', 'pending_review') OR
     coalesce(btrim(p_fields->>'reviewed_by'), '') = '' OR
     coalesce(btrim(p_fields->>'reviewed_at'), '') = '' THEN
    RETURN false;
  END IF;
  new_at := (p_fields->>'reviewed_at')::timestamptz;
  UPDATE public.media_asset SET
    review_status = new_status,
    reviewed_by = p_fields->>'reviewed_by',
    reviewed_at = new_at,
    review_note = p_fields->>'review_note',
    review_content_hash = p_expected_hash,
    consent_status = CASE WHEN p_fields ? 'consent_status'
      THEN p_fields->>'consent_status' ELSE consent_status END,
    release_ref = CASE WHEN p_fields ? 'release_ref'
      THEN p_fields->>'release_ref' ELSE release_ref END,
    consent_member_ref = CASE WHEN p_fields ? 'consent_member_ref'
      THEN p_fields->>'consent_member_ref' ELSE consent_member_ref END,
    consent_expires_at = CASE WHEN p_fields ? 'consent_expires_at'
      THEN (p_fields->>'consent_expires_at')::timestamptz
      ELSE consent_expires_at END
    WHERE id = p_asset_id AND gym_id = p_gym_id;
  INSERT INTO public.media_asset_review_event
    (gym_id, asset_id, content_hash, prior_status, decision,
     reviewed_by, reviewed_at, review_note)
  VALUES (p_gym_id, p_asset_id, p_expected_hash, current_asset.review_status,
          new_status, p_fields->>'reviewed_by', new_at, p_fields->>'review_note');
  RETURN true;
END;
$function$
;

-- release_lasso_backlog_feed_hold
CREATE OR REPLACE FUNCTION public.release_lasso_backlog_feed_hold(p_feed_id uuid, p_story_id uuid, p_expected_feed jsonb, p_expected_story jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  f public.content_calendar%rowtype;
  s public.content_calendar%rowtype;
  v_account text;
  v_day date;
  v_count integer;
begin
  if p_feed_id is null or p_story_id is null or p_feed_id = p_story_id
     or jsonb_typeof(p_expected_feed) <> 'object'
     or jsonb_typeof(p_expected_story) <> 'object'
     or not (p_expected_feed ?& array['id','gym_id','account','post_date',
       'slot_index','format','status','variant_status','caption','image_url',
       'pillar','scheduled_at','logical_post_id','media_not_ready_reason',
       'published_at','late_post_id','publish_claim_token',
       'publish_reservation_day'])
     or not (p_expected_story ?& array['id','gym_id','account','post_date',
       'slot_index','format','status','variant_status','caption','image_url',
       'source_media_url','pillar','scheduled_at','logical_post_id',
       'media_not_ready_reason','published_at','late_post_id',
       'publish_claim_token','publish_reservation_day'])
     or p_expected_feed->>'id' is distinct from p_feed_id::text
     or p_expected_story->>'id' is distinct from p_story_id::text
     or p_expected_feed->>'media_not_ready_reason' is distinct from
        'prepared_backlog_waiting_for_story_and_capacity' then
    return jsonb_build_object('result','conflict','reason','invalid_input');
  end if;
  v_account := lower(btrim(coalesce(p_expected_feed->>'account','')));
  if v_account not in ('instagram','facebook')
     or p_expected_feed->>'gym_id' <> 'lasso'
     or p_expected_story->>'gym_id' <> 'lasso'
     or p_expected_feed->>'post_date' !~ '^2026-10-0[2-5]$'
     or p_expected_feed->>'slot_index' not in ('0','1','2')
     or p_expected_feed->>'status' <> 'pending'
     or p_expected_story->>'status' <> 'pending' then
    return jsonb_build_object('result','conflict','reason','invalid_scope');
  end if;
  v_day := (p_expected_feed->>'post_date')::date;
  perform pg_advisory_xact_lock(hashtextextended(
    'lasso|' || v_account || '|' || v_day::text, 0));
  select * into f from public.content_calendar where id = p_feed_id for update;
  if not found or f.gym_id <> 'lasso' or f.post_date <> v_day
     or lower(btrim(coalesce(f.account,''))) <> v_account
     or lower(btrim(coalesce(f.format,'feed'))) <> 'feed'
     or f.slot_index not in (0,1,2)
     or f.status <> 'pending' or f.variant_status <> 'active'
     or f.published_at is not null or f.late_post_id is not null
     or f.publish_claim_token is not null
     or f.publish_reservation_day is not null
     or nullif(btrim(coalesce(f.caption,'')),'') is null
     or f.image_url !~ '^https://' then
    return jsonb_build_object('result','conflict','reason','feed_unready');
  end if;
  if f.id::text is distinct from p_expected_feed->>'id'
     or f.gym_id is distinct from p_expected_feed->>'gym_id'
     or f.account is distinct from p_expected_feed->>'account'
     or f.post_date::text is distinct from p_expected_feed->>'post_date'
     or f.slot_index::text is distinct from p_expected_feed->>'slot_index'
     or f.format is distinct from p_expected_feed->>'format'
     or f.status is distinct from p_expected_feed->>'status'
     or f.variant_status is distinct from p_expected_feed->>'variant_status'
     or f.caption is distinct from p_expected_feed->>'caption'
     or f.image_url is distinct from p_expected_feed->>'image_url'
     or f.pillar is distinct from p_expected_feed->>'pillar'
     or f.scheduled_at is distinct from
        nullif(p_expected_feed->>'scheduled_at','')::timestamptz
     or f.logical_post_id is distinct from
        nullif(p_expected_feed->>'logical_post_id','')::uuid
     or f.published_at is distinct from
        nullif(p_expected_feed->>'published_at','')::timestamptz
     or f.late_post_id::text is distinct from p_expected_feed->>'late_post_id'
     or f.publish_claim_token is distinct from
        nullif(p_expected_feed->>'publish_claim_token','')::uuid
     or f.publish_reservation_day is distinct from
        nullif(p_expected_feed->>'publish_reservation_day','')::date then
    return jsonb_build_object('result','conflict','reason','feed_changed');
  end if;
  if f.media_not_ready_reason is distinct from
       'prepared_backlog_waiting_for_story_and_capacity'
     and f.media_not_ready_reason is not null then
    return jsonb_build_object('result','conflict','reason','feed_hold_changed');
  end if;
  select count(*) into v_count from public.content_calendar c
   where c.gym_id = 'lasso' and c.post_date = v_day
     and lower(btrim(coalesce(c.account,''))) = v_account
     and lower(btrim(coalesce(c.format,'feed'))) = 'feed'
     and c.slot_index = f.slot_index
     and coalesce(c.variant_status,'active') = 'active';
  if v_count <> 1 then
    return jsonb_build_object('result','conflict','reason','ambiguous_feed_slot');
  end if;
  select * into s from public.content_calendar where id = p_story_id for update;
  if not found or s.gym_id <> 'lasso' or s.post_date <> v_day
     or lower(btrim(coalesce(s.account,''))) <> v_account
     or lower(btrim(coalesce(s.format,'feed'))) <> 'story'
     or s.slot_index is distinct from f.slot_index
     or s.status <> 'pending' or s.variant_status <> 'active'
     or s.published_at is not null or s.late_post_id is not null
     or s.publish_claim_token is not null
     or s.publish_reservation_day is not null
     or s.media_not_ready_reason not in ('paired_feed_not_ready')
        and s.media_not_ready_reason is not null
     or s.image_url !~ '^https://' or s.image_url = f.image_url
     or s.source_media_url is distinct from s.image_url then
    return jsonb_build_object('result','conflict','reason','story_unready');
  end if;
  if s.id::text is distinct from p_expected_story->>'id'
     or s.gym_id is distinct from p_expected_story->>'gym_id'
     or s.account is distinct from p_expected_story->>'account'
     or s.post_date::text is distinct from p_expected_story->>'post_date'
     or s.slot_index::text is distinct from p_expected_story->>'slot_index'
     or s.format is distinct from p_expected_story->>'format'
     or s.status is distinct from p_expected_story->>'status'
     or s.variant_status is distinct from p_expected_story->>'variant_status'
     or s.caption is distinct from p_expected_story->>'caption'
     or s.image_url is distinct from p_expected_story->>'image_url'
     or s.source_media_url is distinct from p_expected_story->>'source_media_url'
     or s.pillar is distinct from p_expected_story->>'pillar'
     or s.scheduled_at is distinct from
        nullif(p_expected_story->>'scheduled_at','')::timestamptz
     or s.logical_post_id is distinct from
        nullif(p_expected_story->>'logical_post_id','')::uuid
     or s.media_not_ready_reason is distinct from
        p_expected_story->>'media_not_ready_reason'
     or s.published_at is distinct from
        nullif(p_expected_story->>'published_at','')::timestamptz
     or s.late_post_id::text is distinct from p_expected_story->>'late_post_id'
     or s.publish_claim_token is distinct from
        nullif(p_expected_story->>'publish_claim_token','')::uuid
     or s.publish_reservation_day is distinct from
        nullif(p_expected_story->>'publish_reservation_day','')::date then
    return jsonb_build_object('result','conflict','reason','story_changed');
  end if;
  select count(*) into v_count from public.content_calendar c
   where c.gym_id = 'lasso' and c.post_date = v_day
     and lower(btrim(coalesce(c.account,''))) = v_account
     and lower(btrim(coalesce(c.format,'feed'))) = 'story'
     and c.slot_index = f.slot_index
     and coalesce(c.variant_status,'active') = 'active'
     and c.status = 'pending';
  if v_count <> 1 or exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso' and c.post_date = v_day
       and lower(btrim(coalesce(c.account,''))) = v_account
       and lower(btrim(coalesce(c.format,'feed'))) = 'story'
       and coalesce(c.variant_status,'active') = 'active'
       and (c.status is null or c.status not in ('denied','killed','failed'))
       and c.id <> s.id
       and (c.slot_index is null or c.slot_index = f.slot_index)
       and (c.status is distinct from 'published'
            or not public.lasso_unrelated_published_story(c.id, f.id))) then
    return jsonb_build_object('result','conflict','reason','ambiguous_story_slot');
  end if;
  if not exists (select 1 from public.lasso_managed_paired_stories m
                  where m.story_id = s.id and m.feed_id = f.id)
     or not public.lasso_story_current_source(s.id) then
    return jsonb_build_object('result','conflict','reason','managed_story_source_unverified');
  end if;
  if f.media_not_ready_reason is null then
    return jsonb_build_object('result','idempotent','feed_id',f.id,'story_id',s.id);
  end if;
  update public.content_calendar
     set media_not_ready_reason = null
   where id = f.id and gym_id = 'lasso'
     and status = 'pending' and variant_status = 'active'
     and media_not_ready_reason =
       'prepared_backlog_waiting_for_story_and_capacity'
     and published_at is null and late_post_id is null
     and publish_claim_token is null and publish_reservation_day is null;
  if not found then
    raise exception 'LASSO backlog feed hold CAS lost';
  end if;
  if not public.lasso_story_current_source(s.id) then
    raise exception 'LASSO backlog Story proof changed during release';
  end if;
  return jsonb_build_object('result','released','feed_id',f.id,'story_id',s.id);
end;
$function$
;

-- release_lasso_paired_story_hold
CREATE OR REPLACE FUNCTION public.release_lasso_paired_story_hold(p_story_id uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  s public.content_calendar%rowtype;
  f public.content_calendar%rowtype;
begin
  select * into s from public.content_calendar
   where id = p_story_id and gym_id = 'lasso'
     and lower(btrim(coalesce(format, 'feed'))) = 'story'
     and status = 'pending' and variant_status = 'active'
     and media_not_ready_reason = 'paired_feed_not_ready'
     and published_at is null and late_post_id is null
     and publish_claim_token is null for update;
  if not found then
    return jsonb_build_object('result','conflict','reason','not_releasable');
  end if;
  if not exists (select 1 from public.lasso_managed_paired_stories m
                 where m.story_id = s.id) then
    return jsonb_build_object('result','conflict','reason','unmanaged_story');
  end if;
  perform pg_advisory_xact_lock(hashtextextended(
    'lasso|' || lower(btrim(s.account)) || '|' || s.post_date::text, 0));
  if not public.lasso_story_current_source(s.id) then
    return jsonb_build_object('result','conflict','reason','source_changed');
  end if;
  select * into f from public.content_calendar
   where gym_id = 'lasso' and post_date = s.post_date
     and lower(btrim(coalesce(account, ''))) = lower(btrim(s.account))
     and lower(btrim(coalesce(format, 'feed'))) = 'feed'
     and slot_index = s.slot_index and variant_status = 'active';
  if not found or f.status <> 'published' or f.published_at is null
     or f.late_post_id is null then
    return jsonb_build_object('result','held','reason','paired_feed_not_ready');
  end if;
  if exists (
    select 1 from public.content_calendar c where c.id <> s.id
      and c.gym_id = 'lasso' and c.post_date = s.post_date
      and lower(btrim(coalesce(c.account, ''))) = lower(btrim(s.account))
      and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
      and c.slot_index = s.slot_index
      and coalesce(c.variant_status, 'active') = 'active'
      and c.status = 'published' and c.published_at is not null
      and c.late_post_id is not null
      and not public.lasso_unrelated_published_story(c.id, f.id)
  ) then
    return jsonb_build_object('result','conflict','reason','historical_story_already_published');
  end if;
  update public.content_calendar set media_not_ready_reason = null
   where id = s.id;
  return jsonb_build_object('result','released','id',s.id,'feed_id',f.id);
end;
$function$
;

-- repair_lasso_paired_story
CREATE OR REPLACE FUNCTION public.repair_lasso_paired_story(p_story_id uuid, p_feed_id uuid, p_expected_story jsonb, p_expected_feed jsonb, p_story_image_url text, p_story_sha256 text, p_artifact_tenant text, p_source_hash text, p_policy_version text, p_story_scheduled_at timestamp with time zone)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  s public.content_calendar%rowtype;
  f public.content_calendar%rowtype;
  v_account text;
  v_reason text;
begin
  if p_story_id is null or p_feed_id is null or p_story_id = p_feed_id
     or jsonb_typeof(p_expected_story) <> 'object'
     or jsonb_typeof(p_expected_feed) <> 'object'
     or p_story_image_url !~ '^https://'
     or p_story_sha256 !~ '^[0-9a-f]{64}$'
     or p_source_hash !~ '^[0-9a-f]{64}$'
     or p_artifact_tenant not in ('lasso', 'lasso_ig', 'lasso_fb')
     or nullif(p_policy_version, '') is null
     or p_story_scheduled_at is null then
    return jsonb_build_object('result','conflict','reason','invalid_input');
  end if;
  select * into s from public.content_calendar
   where id = p_story_id and gym_id = 'lasso'
     and lower(btrim(coalesce(format, 'feed'))) = 'story'
   for update;
  if not found then
    return jsonb_build_object('result','conflict','reason','story_missing');
  end if;
  v_account := lower(btrim(coalesce(s.account, '')));
  if v_account not in ('instagram','facebook')
     or s.post_date < date '2026-10-02'
     or s.slot_index not in (0, 1, 2)
     or s.status <> 'pending' or s.variant_status <> 'active'
     or s.published_at is not null or s.late_post_id is not null
     or s.publish_claim_token is not null
     or s.media_not_ready_reason not in ('paired_feed_not_ready',
                                         'caption_changed_needs_new_visual',
                                         'cross_date_media_repeat_needs_new_visual')
         and s.media_not_ready_reason is not null
     or s.caption is distinct from p_expected_story->>'caption'
     or s.pillar is distinct from p_expected_story->>'pillar'
     or s.image_url is distinct from p_expected_story->>'image_url'
     or s.source_media_url is distinct from p_expected_story->>'source_media_url'
     or s.media_not_ready_reason is distinct from
        p_expected_story->>'media_not_ready_reason'
     or s.scheduled_at is distinct from
        nullif(p_expected_story->>'scheduled_at','')::timestamptz
     or s.logical_post_id is distinct from
        nullif(p_expected_story->>'logical_post_id','')::uuid
     or s.status is distinct from p_expected_story->>'status'
     or s.variant_status is distinct from p_expected_story->>'variant_status'
     or s.post_date::text is distinct from p_expected_story->>'post_date'
     or s.slot_index::text is distinct from p_expected_story->>'slot_index'
     or v_account is distinct from lower(btrim(p_expected_story->>'account')) then
    return jsonb_build_object('result','conflict','reason','story_changed_or_claimed');
  end if;
  perform pg_advisory_xact_lock(hashtextextended(
    'lasso|' || v_account || '|' || s.post_date::text, 0));
  if (
    select count(*) from public.content_calendar c
     where c.gym_id = 'lasso' and c.post_date = s.post_date
       and lower(btrim(coalesce(c.account, ''))) = v_account
       and lower(btrim(coalesce(c.format, 'feed'))) = 'feed'
       and c.slot_index = s.slot_index and c.variant_status = 'active'
  ) <> 1 then
    return jsonb_build_object('result','conflict','reason','ambiguous_feed_slot');
  end if;
  select * into f from public.content_calendar
   where id = p_feed_id and gym_id = 'lasso'
     and lower(btrim(coalesce(account, ''))) = v_account
     and post_date = s.post_date and slot_index = s.slot_index
     and lower(btrim(coalesce(format, 'feed'))) = 'feed'
     and variant_status = 'active' for update;
  if not found or f.status not in ('pending','approved','published')
     or f.caption is distinct from p_expected_feed->>'caption'
     or f.image_url is distinct from p_expected_feed->>'image_url'
     or f.pillar is distinct from p_expected_feed->>'pillar'
     or f.status is distinct from p_expected_feed->>'status'
     or f.scheduled_at is distinct from
        nullif(p_expected_feed->>'scheduled_at','')::timestamptz
     or f.logical_post_id is distinct from
        nullif(p_expected_feed->>'logical_post_id','')::uuid
     or (f.media_not_ready_reason is not null and not
         (f.media_not_ready_reason =
          'prepared_backlog_waiting_for_story_and_capacity'
          and f.status = 'pending'
          and f.post_date between date '2026-10-02' and date '2026-10-05'))
     or (f.status = 'published' and
         (f.published_at is null or f.late_post_id is null))
     or (f.status in ('pending','approved') and
         (f.published_at is not null or f.late_post_id is not null))
     or s.logical_post_id is distinct from f.logical_post_id
     or p_story_image_url = f.image_url
     or (p_story_scheduled_at at time zone 'America/New_York')::date <> s.post_date
     or (f.scheduled_at is not null and
         p_story_scheduled_at <> f.scheduled_at + interval '15 minutes') then
    return jsonb_build_object('result','conflict','reason','feed_changed_or_pairing_mismatch');
  end if;
  if p_artifact_tenant = 'lasso_ig' and v_account <> 'instagram'
     or p_artifact_tenant = 'lasso_fb' and v_account <> 'facebook'
     or not exists (
       select 1 from public.echo_infographic_artifacts a
        where a.tenant = p_artifact_tenant
          and a.image_url = p_story_image_url
          and a.image_sha256 = p_story_sha256
          and a.evidence->>'grade_status' = 'PASS'
          and a.evidence->>'image_sha256' = p_story_sha256
          and a.evidence->>'policy_version' = p_policy_version
          and a.evidence->>'aspect' = '9:16'
          and a.evidence->>'pixels' = '1080x1920'
          and a.evidence->'verified_dimensions'->>'width' = '1080'
          and a.evidence->'verified_dimensions'->>'height' = '1920'
          and a.evidence->'verified_dimensions'->>'image_sha256' = p_story_sha256
          and a.source_identity->>'source_hash' = p_source_hash
     ) or not public.lasso_story_review_matches(f.id, p_story_image_url) then
    return jsonb_build_object('result','conflict','reason','reviewed_source_mismatch');
  end if;
  if exists (
    select 1 from public.content_calendar c where c.id <> s.id
      and c.gym_id = 'lasso'
      and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
      and c.variant_status = 'active'
      and c.status in ('pending','approved','publishing')
      and c.image_url = p_story_image_url
  ) then
    return jsonb_build_object('result','conflict','reason','story_media_already_in_use');
  end if;
  if exists (
    select 1 from public.content_calendar c where c.id <> s.id
      and c.gym_id = 'lasso' and c.post_date = s.post_date
      and lower(btrim(coalesce(c.account, ''))) = v_account
      and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
      and c.slot_index = s.slot_index
      and coalesce(c.variant_status, 'active') = 'active'
      and c.status = 'published' and c.published_at is not null
      and c.late_post_id is not null
      and not public.lasso_unrelated_published_story(c.id, f.id)
  ) then
    return jsonb_build_object('result','conflict','reason','historical_story_already_published');
  end if;
  v_reason := case when f.status = 'published' then null
                   else 'paired_feed_not_ready' end;
  update public.content_calendar
     set pillar = f.pillar, caption = '', image_url = p_story_image_url,
         source_media_url = p_story_image_url,
         scheduled_at = p_story_scheduled_at,
         logical_post_id = f.logical_post_id,
         media_not_ready_reason = v_reason
   where id = s.id;
  if not public.lasso_story_current_source(s.id) then
    raise exception 'repaired Story failed source verification';
  end if;
  insert into public.lasso_managed_paired_stories(story_id, feed_id)
  values (s.id, f.id)
  on conflict (story_id) do nothing;
  if not exists (select 1 from public.lasso_managed_paired_stories m
                 where m.story_id = s.id and m.feed_id = f.id) then
    raise exception 'managed Story bound to another feed';
  end if;
  return jsonb_build_object('result','repaired','id',s.id,'feed_id',f.id,
    'hold_reason',v_reason);
end;
$function$
;

-- request_gym_media_sync
CREATE OR REPLACE FUNCTION public.request_gym_media_sync(p_source_id text, p_gym_id text)
 RETURNS boolean
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE changed integer;
BEGIN
  UPDATE media_source SET sync_requested_at = clock_timestamp(),
    sync_status = CASE WHEN sync_status = 'indexing' THEN 'indexing' ELSE 'queued' END,
    sync_error = NULL
  WHERE id = p_source_id AND gym_id = p_gym_id AND active AND kind = 'gym_drive';
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed > 0;
END $function$
;

-- stage_lasso_campaign_row
CREATE OR REPLACE FUNCTION public.stage_lasso_campaign_row(p_row jsonb, p_artifact_tenant text, p_source_hash text, p_policy_version text, p_image_sha256 text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_id uuid;
  v_day date;
  v_account text;
  v_slot integer;
  v_image_url text;
  v_caption text;
  v_scheduled_at timestamptz;
  v_existing public.content_calendar%rowtype;
begin
  if p_row is null or jsonb_typeof(p_row) <> 'object' then
    return jsonb_build_object('result', 'conflict', 'reason', 'invalid_row');
  end if;

  -- Reject invented/unknown provenance and every calendar field this narrow
  -- staging path is not allowed to write.  In particular, the production
  -- calendar schema has no category or draft_type column.
  if exists (
    select 1 from jsonb_object_keys(p_row) as supplied(key)
    where supplied.key not in (
      'id', 'gym_id', 'account', 'post_date', 'pillar', 'format', 'caption',
      'image_url', 'status', 'scheduled_at', 'slot_index', 'variant_status'
    )
  ) then
    return jsonb_build_object('result', 'conflict', 'reason', 'unsupported_row_key');
  end if;

  begin
    v_id := nullif(p_row->>'id', '')::uuid;
    v_day := nullif(p_row->>'post_date', '')::date;
    v_slot := nullif(p_row->>'slot_index', '')::integer;
    v_scheduled_at := nullif(p_row->>'scheduled_at', '')::timestamptz;
  exception when others then
    return jsonb_build_object('result', 'conflict', 'reason', 'invalid_typed_value');
  end;
  v_account := lower(btrim(coalesce(p_row->>'account', '')));
  v_image_url := btrim(coalesce(p_row->>'image_url', ''));
  v_caption := coalesce(p_row->>'caption', '');

  if v_id is null
      or v_day is null
      or p_row->>'gym_id' is distinct from 'lasso'
      or v_day not between date '2026-09-23' and date '2026-11-08'
      or v_account not in ('instagram', 'facebook')
      or lower(btrim(coalesce(p_row->>'format', ''))) <> 'feed'
      or lower(btrim(coalesce(p_row->>'status', ''))) <> 'pending'
      or lower(btrim(coalesce(p_row->>'variant_status', ''))) <> 'active'
      or v_slot is null
      or v_slot not in (0, 1, 2)
      or nullif(btrim(coalesce(p_row->>'pillar', '')), '') is null
      or nullif(v_caption, '') is null
      or nullif(v_image_url, '') is null
      or nullif(btrim(coalesce(p_artifact_tenant, '')), '') is null
      or p_artifact_tenant not in ('lasso', 'lasso_ig')
      or p_source_hash is null
      or p_source_hash !~ '^[0-9a-f]{64}$'
      or p_image_sha256 is null
      or p_image_sha256 !~ '^[0-9a-f]{64}$'
      or nullif(btrim(coalesce(p_policy_version, '')), '') is null then
    return jsonb_build_object('result', 'conflict', 'reason', 'out_of_scope');
  end if;
  if (v_slot = 2) <> (lower(btrim(p_row->>'pillar')) = 'summit') then
    return jsonb_build_object('result', 'conflict', 'reason', 'pillar_slot_mismatch');
  end if;

  -- Serialize against every ordinary content_calendar writer, not only callers
  -- that voluntarily take the campaign advisory lock.
  lock table public.content_calendar in share row exclusive mode;

  if not exists (
    select 1
      from public.echo_infographic_artifacts a
     where a.tenant = p_artifact_tenant
       and a.image_url = v_image_url
       and a.image_sha256 = p_image_sha256
       and a.evidence->>'grade_status' = 'PASS'
       and a.evidence->>'image_sha256' = p_image_sha256
       and a.evidence->>'policy_version' = p_policy_version
       and a.source_identity->>'source_hash' = p_source_hash
  ) then
    return jsonb_build_object('result', 'conflict', 'reason', 'reviewed_artifact_mismatch');
  end if;

  select * into v_existing
    from public.content_calendar
   where id = v_id;
  if found then
    if v_existing.gym_id = 'lasso'
       and lower(btrim(coalesce(v_existing.account, ''))) = v_account
       and v_existing.post_date = v_day
       and lower(btrim(coalesce(v_existing.format, 'feed'))) = 'feed'
       and v_existing.status = 'pending'
       and v_existing.variant_status = 'active'
       and v_existing.slot_index = v_slot
       and v_existing.pillar = p_row->>'pillar'
       and v_existing.caption = v_caption
       and v_existing.image_url = v_image_url
       and v_existing.scheduled_at is not distinct from v_scheduled_at then
      return jsonb_build_object('result', 'idempotent', 'id', v_id);
    end if;
    return jsonb_build_object('result', 'conflict', 'reason', 'id_reused_with_different_row', 'id', v_id);
  end if;

  -- An active occupying row owns its account/day/slot. Protected rows are read
  -- as conflicts and are never modified by this function.
  if exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso'
       and c.post_date = v_day
       and lower(btrim(coalesce(c.account, ''))) = v_account
       and lower(btrim(coalesce(c.format, 'feed'))) = 'feed'
       and c.variant_status = 'active'
       and c.status in ('pending', 'approved', 'publishing', 'published', 'draft')
       -- A legacy active row with no ordinal makes the logical shape ambiguous;
       -- fail closed until it is separately normalized under review.
       and (c.slot_index = v_slot or c.slot_index is null)
  ) then
    return jsonb_build_object('result', 'conflict', 'reason', 'occupied_logical_slot');
  end if;

  -- Instagram and Facebook are the two rows of one logical creative and may
  -- share media/copy only when date and slot match. Reuse by the same account,
  -- another logical slot, or another campaign day is refused.
  if exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso'
       and lower(btrim(coalesce(c.format, 'feed'))) = 'feed'
       and c.variant_status = 'active'
       and c.status in ('pending', 'approved', 'publishing', 'published', 'draft')
       and c.post_date between date '2026-09-23' and date '2026-11-08'
       and (c.image_url = v_image_url or c.caption = v_caption)
       and not (
         c.post_date = v_day
         and c.slot_index = v_slot
         and lower(btrim(coalesce(c.account, ''))) in ('instagram', 'facebook')
         and lower(btrim(coalesce(c.account, ''))) <> v_account
       )
  ) then
    return jsonb_build_object('result', 'conflict', 'reason', 'cross_day_creative_reuse');
  end if;

  insert into public.content_calendar (
    id, gym_id, account, post_date, pillar, format, caption, image_url,
    status, scheduled_at, slot_index, variant_status
  ) values (
    v_id, 'lasso', v_account, v_day, p_row->>'pillar', 'feed', v_caption,
    v_image_url, 'pending', v_scheduled_at,
    v_slot, 'active'
  );

  return jsonb_build_object('result', 'inserted', 'id', v_id);
exception
  when unique_violation then
    -- Defensive only: the table lock makes an ordinary concurrent insert wait.
    return jsonb_build_object('result', 'conflict', 'reason', 'unique_violation');
end;
$function$
;

-- stage_lasso_paired_story
CREATE OR REPLACE FUNCTION public.stage_lasso_paired_story(p_feed_id uuid, p_account text, p_day date, p_slot integer, p_feed_status text, p_feed_caption text, p_feed_image_url text, p_feed_scheduled_at timestamp with time zone, p_feed_logical_post_id uuid, p_story_id uuid, p_story_image_url text, p_story_sha256 text, p_artifact_tenant text, p_source_hash text, p_policy_version text, p_story_scheduled_at timestamp with time zone)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_feed public.content_calendar%rowtype;
  v_existing public.content_calendar%rowtype;
  v_account text;
begin
  v_account := lower(btrim(coalesce(p_account, '')));
  if p_feed_id is null or p_story_id is null or p_feed_id = p_story_id
     or v_account not in ('instagram', 'facebook')
     or p_day < date '2026-10-02'
     or p_slot not in (0, 1, 2)
     or p_feed_status not in ('pending', 'approved', 'published')
     or nullif(btrim(coalesce(p_feed_caption, '')), '') is null
     or p_feed_image_url !~ '^https://'
     or p_story_image_url !~ '^https://'
     or p_feed_image_url = p_story_image_url
     or p_story_sha256 is null or p_story_sha256 !~ '^[0-9a-f]{64}$'
     or p_artifact_tenant not in ('lasso', 'lasso_ig', 'lasso_fb')
     or (p_artifact_tenant = 'lasso_ig' and v_account <> 'instagram')
     or (p_artifact_tenant = 'lasso_fb' and v_account <> 'facebook')
     or p_source_hash is null or p_source_hash !~ '^[0-9a-f]{64}$'
     or nullif(btrim(coalesce(p_policy_version, '')), '') is null
     or p_story_scheduled_at is null
     or (p_story_scheduled_at at time zone 'America/New_York')::date <> p_day
     or (p_feed_scheduled_at is not null and
         p_story_scheduled_at <> p_feed_scheduled_at + interval '15 minutes') then
    return jsonb_build_object('result', 'conflict', 'reason', 'invalid_input');
  end if;

  -- LASSO account/day only. The trigger above takes this same key for legacy
  -- NULL-slot writers; the unique indexes settle exact-slot races.
  perform pg_advisory_xact_lock(hashtextextended(
    'lasso|' || v_account || '|' || p_day::text, 0));
  select * into v_feed from public.content_calendar
   where id = p_feed_id and gym_id = 'lasso'
     and lower(btrim(coalesce(account, ''))) = v_account
     and post_date = p_day
     and lower(btrim(coalesce(format, 'feed'))) = 'feed'
     and slot_index = p_slot and variant_status = 'active'
   for update;
  if not found then
    return jsonb_build_object('result', 'conflict', 'reason', 'source_feed_missing');
  end if;
  if v_feed.status is distinct from p_feed_status
     or v_feed.caption is distinct from p_feed_caption
     or v_feed.image_url is distinct from p_feed_image_url
     or v_feed.scheduled_at is distinct from p_feed_scheduled_at
     or v_feed.logical_post_id is distinct from p_feed_logical_post_id
     or (v_feed.media_not_ready_reason is not null and not
         (v_feed.media_not_ready_reason =
          'prepared_backlog_waiting_for_story_and_capacity'
          and v_feed.status = 'pending'
          and v_feed.post_date between date '2026-10-02' and date '2026-10-05'))
     or (v_feed.status = 'published' and
         (v_feed.published_at is null or v_feed.late_post_id is null))
     or (v_feed.status in ('pending', 'approved') and
         (v_feed.published_at is not null or v_feed.late_post_id is not null)) then
    return jsonb_build_object('result', 'conflict', 'reason', 'source_feed_changed_or_unready');
  end if;

  -- The reviewed Story record must name this exact account's feed row, copy,
  -- media, day and ordinal. A same-date Instagram Story cannot impersonate
  -- Facebook's independently written caption or its feed identity.
  if not exists (
    select 1 from public.echo_infographic_artifacts a
     where a.tenant = p_artifact_tenant and a.image_url = p_story_image_url
       and a.image_sha256 = p_story_sha256
       and a.evidence->>'grade_status' = 'PASS'
       and a.evidence->>'image_sha256' = p_story_sha256
       and a.evidence->>'policy_version' = p_policy_version
       and a.evidence->>'aspect' = '9:16'
       and a.evidence->>'pixels' = '1080x1920'
       and a.evidence->'verified_dimensions'->>'width' = '1080'
       and a.evidence->'verified_dimensions'->>'height' = '1920'
       and a.evidence->'verified_dimensions'->>'image_sha256' = p_story_sha256
       and a.source_identity->>'source_hash' = p_source_hash
       and (
         (p_artifact_tenant = 'lasso'
          and a.source_identity->>'source_feed_id' = p_feed_id::text
          and a.source_identity->>'source_id' =
              'content_calendar:' || p_feed_id::text || ':paired_story'
          and a.source_identity->>'source_account' = v_account
          and a.source_identity->>'source_day' = p_day::text
          and a.source_identity->>'source_slot' = p_slot::text
          and a.source_identity->>'source_feed_caption' = p_feed_caption
          and a.source_identity->>'source_feed_image_url' = p_feed_image_url
          and a.source_identity->>'source_logical_post_id' =
              coalesce(p_feed_logical_post_id::text, ''))
         or
         (p_artifact_tenant in ('lasso_ig', 'lasso_fb')
          and a.source_identity->>'source_id' =
              'content_calendar:' || p_feed_id::text || ':caption'
          and p_source_hash =
              encode(sha256(convert_to(p_feed_caption, 'UTF8')), 'hex'))
       )
  ) then
    return jsonb_build_object('result', 'conflict', 'reason', 'reviewed_9x16_source_mismatch');
  end if;

  select * into v_existing from public.content_calendar where id = p_story_id;
  if found then
    if v_existing.gym_id = 'lasso'
       and lower(btrim(coalesce(v_existing.account, ''))) = v_account
       and v_existing.post_date = p_day
       and lower(btrim(coalesce(v_existing.format, 'feed'))) = 'story'
       and v_existing.slot_index = p_slot
       and v_existing.variant_status = 'active'
       and v_existing.status in ('pending', 'approved')
       and v_existing.caption = ''
       and v_existing.image_url = p_story_image_url
       and v_existing.source_media_url = p_story_image_url
       and v_existing.logical_post_id is not distinct from v_feed.logical_post_id
       and v_existing.scheduled_at is not distinct from p_story_scheduled_at
       and v_existing.media_not_ready_reason is null then
      insert into public.lasso_managed_paired_stories(story_id, feed_id)
      values (p_story_id, p_feed_id)
      on conflict (story_id) do nothing;
      if not exists (select 1 from public.lasso_managed_paired_stories m
                     where m.story_id = p_story_id and m.feed_id = p_feed_id) then
        return jsonb_build_object('result', 'conflict', 'reason', 'managed_pair_conflict');
      end if;
      return jsonb_build_object('result', 'idempotent', 'id', p_story_id);
    end if;
    return jsonb_build_object('result', 'conflict', 'reason', 'id_reused');
  end if;

  if exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso' and c.post_date = p_day
       and lower(btrim(coalesce(c.account, ''))) = v_account
       and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
       and coalesce(c.variant_status, 'active') = 'active'
       and coalesce(c.status, 'pending') not in ('denied', 'killed', 'failed')
       and (c.slot_index = p_slot or c.slot_index is null)
       and (c.status <> 'published' or c.slot_index is null
            or not public.lasso_unrelated_published_story(c.id, p_feed_id))
  ) then
    return jsonb_build_object('result', 'conflict', 'reason', 'occupied_story_slot');
  end if;
  if exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso'
       and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
       and coalesce(c.variant_status, 'active') = 'active'
       and coalesce(c.status, 'pending') not in ('denied', 'killed', 'failed')
       and c.image_url = p_story_image_url
  ) then
    return jsonb_build_object('result', 'conflict', 'reason', 'story_media_already_in_use');
  end if;

  insert into public.content_calendar
    (id, gym_id, account, post_date, pillar, format, caption, image_url,
     source_media_url, status, scheduled_at, slot_index, variant_status,
     logical_post_id)
  values
    (p_story_id, 'lasso', v_account, p_day, v_feed.pillar, 'story', '',
     p_story_image_url, p_story_image_url, 'pending', p_story_scheduled_at,
     p_slot, 'active', v_feed.logical_post_id);
  insert into public.lasso_managed_paired_stories(story_id, feed_id)
  values (p_story_id, p_feed_id);
  return jsonb_build_object('result', 'inserted', 'id', p_story_id,
    'feed_id', p_feed_id, 'account', v_account, 'day', p_day, 'slot', p_slot);
exception when unique_violation then
  return jsonb_build_object('result', 'conflict', 'reason', 'unique_violation');
end;
$function$
;

-- stage_lasso_third_story
CREATE OR REPLACE FUNCTION public.stage_lasso_third_story(p_feed_id uuid, p_feed_caption text, p_feed_image_url text, p_story_id uuid, p_story_image_url text, p_story_source_url text, p_story_sha256 text, p_source_hash text, p_policy_version text, p_scheduled_at timestamp with time zone, p_caption_hash text DEFAULT NULL::text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_feed public.content_calendar%rowtype;
  v_existing public.content_calendar%rowtype;
  v_day date;
begin
  if p_feed_id is null or p_story_id is null or p_feed_id = p_story_id
     or nullif(btrim(coalesce(p_feed_caption, '')), '') is null
     or nullif(btrim(coalesce(p_feed_image_url, '')), '') is null
     or nullif(btrim(coalesce(p_story_image_url, '')), '') is null
     or nullif(btrim(coalesce(p_story_source_url, '')), '') is null
     or p_story_image_url = p_feed_image_url
     or p_story_source_url <> p_story_image_url
     or p_story_sha256 is null or p_source_hash is null
     or p_story_sha256 !~ '^[0-9a-f]{64}$'
     or p_source_hash !~ '^[0-9a-f]{64}$'
     or (p_caption_hash is not null and p_caption_hash !~ '^[0-9a-f]{64}$')
     or (p_caption_hash is not null and p_source_hash <> p_caption_hash)
     or nullif(btrim(coalesce(p_policy_version, '')), '') is null
     or p_scheduled_at is null then
    return jsonb_build_object('result','conflict','reason','invalid_input');
  end if;

  -- The source row lock, per-day advisory lock, and unique active slot index
  -- coordinate this insert without blocking calendar writers for other gyms.
  select * into v_feed from public.content_calendar
   where id = p_feed_id and gym_id = 'lasso' and lower(btrim(account)) = 'instagram'
     and lower(btrim(coalesce(format, 'feed'))) = 'feed'
     and slot_index = 2 and variant_status = 'active'
   for update;
  if not found then
    return jsonb_build_object('result','conflict','reason','source_feed_missing');
  end if;
  v_day := v_feed.post_date;
  perform pg_advisory_xact_lock(hashtextextended('lasso|instagram|' || v_day::text, 0));
  if v_day not between date '2026-09-23' and date '2026-11-08'
     or v_feed.status not in ('pending','approved','publishing','published')
     or v_feed.caption is distinct from p_feed_caption
     or v_feed.image_url is distinct from p_feed_image_url
     or lower(btrim(coalesce(v_feed.pillar, ''))) <> 'summit'
     or (p_scheduled_at at time zone 'America/New_York')::date <> v_day then
    return jsonb_build_object('result','conflict','reason','source_feed_changed_or_out_of_scope');
  end if;
  -- The reviewed feed artifact must carry the exact provenance hash: the
  -- approved catalog hash (catalog path) or the exact feed caption hash
  -- (LIVE-feed path).
  if not exists (
    select 1 from public.echo_infographic_artifacts a
     where a.tenant in ('lasso', 'lasso_ig') and a.image_url = p_feed_image_url
       and a.source_identity->>'source_hash' = p_source_hash
       and (p_caption_hash is null or
            a.source_identity->>'source_id' =
              'content_calendar:' || p_feed_id::text || ':caption')
       and a.evidence->>'grade_status' = 'PASS'
       and a.evidence->>'image_sha256' = a.image_sha256
  ) then
    return jsonb_build_object('result','conflict','reason','reviewed_feed_source_mismatch');
  end if;

  -- A Story must have its own reviewed 9:16 delivered object whose recorded
  -- source is this exact feed row and the same exact provenance hash: the
  -- approved catalog hash (catalog path) or the exact feed caption hash
  -- (LIVE-feed path; a changed caption or URL invalidates the story).
  if not exists (
    select 1 from public.echo_infographic_artifacts a
     where a.tenant = 'lasso' and a.image_url = p_story_image_url
       and a.image_sha256 = p_story_sha256
       and a.evidence->>'grade_status' = 'PASS'
       and a.evidence->>'image_sha256' = p_story_sha256
       and a.evidence->>'policy_version' = p_policy_version
       and a.evidence->>'aspect' = '9:16'
       and a.source_identity->>'source_feed_id' = p_feed_id::text
       and a.source_identity->>'source_feed_image_url' = p_feed_image_url
       and a.source_identity->>'source_hash' = p_source_hash
  ) then
    return jsonb_build_object('result','conflict','reason','reviewed_9x16_source_mismatch');
  end if;

  select * into v_existing from public.content_calendar where id = p_story_id;
  if found then
    if v_existing.gym_id = 'lasso' and lower(btrim(v_existing.account)) = 'instagram'
       and v_existing.post_date = v_day and lower(btrim(v_existing.format)) = 'story'
       and v_existing.slot_index = 2 and v_existing.variant_status = 'active'
       and v_existing.caption = '' and v_existing.image_url = p_story_image_url
       and v_existing.source_media_url = p_story_source_url
       and v_existing.status in ('pending','approved','publishing','published')
       and v_existing.media_not_ready_reason is null
       and v_existing.logical_post_id is not distinct from v_feed.logical_post_id
       and v_existing.scheduled_at is not distinct from p_scheduled_at then
      return jsonb_build_object('result','idempotent','id',p_story_id);
    end if;
    return jsonb_build_object('result','conflict','reason','id_reused');
  end if;

  -- A legacy NULL ordinal or an already held Story also owns the logical slot.
  if exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso' and c.post_date = v_day
       and lower(btrim(coalesce(c.account, ''))) = 'instagram'
       and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
       and c.variant_status = 'active'
       and c.status not in ('denied','killed','failed')
       and (c.slot_index = 2 or c.slot_index is null)
  ) then
    return jsonb_build_object('result','conflict','reason','occupied_story_slot');
  end if;
  if exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso' and lower(btrim(coalesce(c.account, ''))) = 'instagram'
       and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
       and c.variant_status = 'active'
       and c.status not in ('denied','killed','failed')
       and c.image_url = p_story_image_url
  ) then
    return jsonb_build_object('result','conflict','reason','story_media_already_in_use');
  end if;

  insert into public.content_calendar
    (id,gym_id,account,post_date,pillar,format,caption,image_url,
     source_media_url,status,scheduled_at,slot_index,variant_status,logical_post_id)
  values
    (p_story_id,'lasso','instagram',v_day,'summit','story','',p_story_image_url,
     p_story_source_url,'pending',p_scheduled_at,2,'active',v_feed.logical_post_id);
  return jsonb_build_object('result','inserted','id',p_story_id,'feed_id',p_feed_id,
                            'logical_post_id',v_feed.logical_post_id);
exception when unique_violation then
  return jsonb_build_object('result','conflict','reason','unique_violation');
end;
$function$
;
