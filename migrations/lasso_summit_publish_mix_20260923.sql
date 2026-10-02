-- Preserve two regular LASSO feeds plus one Summit feed for each campaign day.
-- Apply after lasso_summit_daily_capacity_20260923.sql and before enabling
-- AGENT_LASSO_SUMMIT_DAILY_ENABLED. The claim remains serialized per gym.
alter table public.content_calendar
  drop constraint if exists content_calendar_slot_index_check;
alter table public.content_calendar
  add constraint content_calendar_slot_index_check check (
    slot_index is null or slot_index in (0, 1) or (
      slot_index = 2
      and gym_id = 'lasso'
      and post_date between date '2026-09-23' and date '2026-11-08'
      and coalesce(nullif(lower(btrim(format)), ''), 'feed') = 'feed'
      and lower(btrim(coalesce(pillar, ''))) = 'summit'
    )
  );

create or replace function public.claim_calendar_publish_slot_owned(
  p_row_id uuid, p_gym_id text, p_day date, p_timezone text,
  p_capacity integer, p_approved_only boolean
) returns uuid
language plpgsql security definer set search_path = public
as $$
declare
  v_row public.content_calendar%rowtype;
  v_used integer;
  v_regular_used integer;
  v_summit_used integer;
  v_token uuid;
begin
  if p_capacity < 1 or p_capacity > 3 or p_day is null or p_timezone is null
      or nullif(btrim(p_gym_id), '') is null then
    return null;
  end if;
  -- Capacity 3 is the LASSO Summit daily extra ONLY: canonical gym_id 'lasso',
  -- reservation day inside the explicit 2026-09-23..2026-11-08 campaign window.
  -- Outside that exact envelope a caller asking for 3 is refused outright.
  if p_capacity = 3 and not (p_gym_id = 'lasso'
      and p_day between date '2026-09-23' and date '2026-11-08') then
    return null;
  end if;
  -- Serialize workers for one gym across local-day boundaries.
  perform pg_advisory_xact_lock(hashtextextended(p_gym_id, 0));
  select * into v_row from public.content_calendar
    where id = p_row_id and gym_id = p_gym_id
      and status in ('pending', 'approved') and published_at is null
      and late_post_id is null and variant_status = 'active'
    for update;
  if not found or (p_approved_only and v_row.status <> 'approved') then
    return null;
  end if;
  -- A staged third slot cannot publish while the daily feature flag is off.
  if v_row.slot_index = 2 and p_capacity <> 3 then
    return null;
  end if;
  if p_capacity = 3 then
    if coalesce(nullif(lower(btrim(v_row.format)), ''), 'feed') <> 'feed'
        or not (
          (v_row.slot_index = 2
              and v_row.post_date between date '2026-09-23' and date '2026-11-08'
              and lower(btrim(coalesce(v_row.pillar, ''))) = 'summit')
          or (v_row.slot_index in (0, 1)
              and lower(btrim(coalesce(v_row.pillar, ''))) <> 'summit')
        ) then
      return null;
    end if;
  end if;

  select count(*),
         count(*) filter (where
           slot_index is distinct from 2
           or lower(btrim(coalesce(pillar, ''))) <> 'summit'
         ),
         count(*) filter (where
           slot_index = 2 and lower(btrim(coalesce(pillar, ''))) = 'summit'
         )
    into v_used, v_regular_used, v_summit_used
    from public.content_calendar
    where gym_id = p_gym_id
      and lower(btrim(coalesce(account, ''))) =
          lower(btrim(coalesce(v_row.account, '')))
      and coalesce(nullif(lower(btrim(format)), ''), 'feed') =
          coalesce(nullif(lower(btrim(v_row.format)), ''), 'feed')
      and (status = 'publishing'
           or (status = 'published' and
               (publish_reservation_day = p_day
                or (published_at is not null and
                    (published_at at time zone p_timezone)::date = p_day))));
  if v_used >= p_capacity then
    return null;
  end if;
  if p_capacity = 3 and (
      (v_row.slot_index = 2 and v_summit_used >= 1)
      or (v_row.slot_index in (0, 1) and v_regular_used >= 2)
  ) then
    return null;
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
