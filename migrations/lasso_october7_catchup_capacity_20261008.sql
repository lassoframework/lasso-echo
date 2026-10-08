-- A dated, LASSO-only catchup envelope for the October 7 backlog day.
-- The normal per-account/per-format limit remains three. On the October 8-9
-- America/New_York publish days only, allow three additional posts dated
-- exactly October 7 per account and format alongside the three current-day
-- posts (capacity six, aggregate 24 across the four IG/FB feed/story lanes).
-- Caller must pass capacity six only in this window; all other tenants and
-- capacity values retain the preceding owned, media-guarded claim behavior.
begin;

create or replace function public.claim_calendar_publish_slot_owned(
  p_row_id uuid, p_gym_id text, p_day date, p_timezone text,
  p_capacity integer, p_approved_only boolean,
  p_require_approval_proof boolean DEFAULT false
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
      or p_capacity < 1 or (p_capacity > 3 and p_capacity not in (5, 6, 15))
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
  if p_capacity = 6 and not (
      p_gym_id = 'lasso'
      and p_day between date '2026-10-08' and date '2026-10-09'
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
  if p_capacity in (5, 6, 15) and
      (v_row.post_date is null
       or nullif(btrim(coalesce(v_row.account, '')), '') is null) then
    return null;
  end if;
  if p_capacity in (3, 5, 6, 15) and
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
  if p_capacity = 6 and (
      (v_row.post_date = p_day
       or (v_row.post_date = date '2026-10-07' and v_row.post_date < p_day))
      and lower(btrim(coalesce(v_row.account, ''))) in ('instagram', 'facebook')
      and lower(btrim(v_row.format)) in ('feed', 'story')
      and v_row.slot_index in (0, 1, 2)) is not true then
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

  if p_capacity = 6 then
    -- Three current-day slots plus three October 7 backlog slots on the
    -- October 8-9 publish days. The tenant advisory lock serializes both
    -- class counts, and October 7 is strictly before either publish day.
    select count(*) filter (where post_date = p_day),
           count(*) filter (where post_date = date '2026-10-07')
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
        or (v_row.post_date < p_day and v_backlog_used >= 3) then
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
$$;

revoke all on function public.claim_calendar_publish_slot_owned(uuid, text, date, text, integer, boolean, boolean)
  from public, anon, authenticated;
grant execute on function public.claim_calendar_publish_slot_owned(uuid, text, date, text, integer, boolean, boolean)
  to service_role;
commit;
