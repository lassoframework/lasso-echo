-- A dated, LASSO-only catchup envelope for the October 2–5 outage.
-- The normal per-account/per-format limit remains three. From October 6
-- through October 11, allow two additional posts from those four backlog
-- dates per account and format on each actual America/New_York publish day.
-- Caller must pass capacity five only in this window; all other tenants and
-- capacity values retain the preceding owned, media-guarded claim behavior.
begin;

create or replace function public.claim_calendar_publish_slot_owned(
  p_row_id uuid, p_gym_id text, p_day date, p_timezone text,
  p_capacity integer, p_approved_only boolean
) returns uuid
language plpgsql security definer set search_path = public
as $$
declare
  v_row public.content_calendar%rowtype;
  v_used integer;
  v_current_used integer;
  v_backlog_used integer;
  v_token uuid;
begin
  if p_capacity < 1 or (p_capacity > 3 and p_capacity <> 5)
      or p_day is null or p_timezone is null
      or nullif(btrim(p_gym_id), '') is null then
    return null;
  end if;
  if p_capacity = 3 and p_gym_id <> 'lasso' then
    return null;
  end if;
  if p_capacity = 5 and not (
      p_gym_id = 'lasso'
      and p_day between date '2026-10-06' and date '2026-10-11'
      and p_timezone = 'America/New_York') then
    return null;
  end if;

  perform pg_advisory_xact_lock(hashtextextended(p_gym_id, 0));
  select * into v_row from public.content_calendar
    where id = p_row_id and gym_id = p_gym_id
      and status in ('pending', 'approved') and published_at is null
      and late_post_id is null and variant_status = 'active'
      and nullif(btrim(coalesce(image_url, '')), '') is not null
      and media_not_ready_reason is null
    for update;
  if not found or (p_approved_only and v_row.status <> 'approved') then
    return null;
  end if;
  if p_capacity in (3, 5) and
      coalesce(nullif(lower(btrim(v_row.format)), ''), 'feed') not in ('feed', 'story') then
    return null;
  end if;
  if p_capacity = 5 and not (
      v_row.post_date = p_day
      or v_row.post_date between date '2026-10-02' and date '2026-10-05') then
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

  if p_capacity = 5 then
    -- Both classes have their own ceiling. A current-day claim cannot consume
    -- the two outage slots, and a catchup claim cannot consume the three
    -- current-day slots. The tenant advisory lock serializes these counts.
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

  v_token := gen_random_uuid();
  update public.content_calendar
    set status = 'publishing', publish_reservation_day = p_day,
        publish_claim_token = v_token
    where id = p_row_id;
  return v_token;
end;
$$;

revoke all on function public.claim_calendar_publish_slot_owned(uuid, text, date, text, integer, boolean)
  from public, anon, authenticated;
grant execute on function public.claim_calendar_publish_slot_owned(uuid, text, date, text, integer, boolean)
  to service_role;
commit;
