-- Reserve a client's actual local publish day atomically with the row claim.
-- Apply before deploying code that calls claim_calendar_publish_slot. A missing RPC
-- causes the publisher to hold rows, never to fall back to an unsafe split read.
alter table public.content_calendar
  add column if not exists publish_reservation_day date;

create or replace function public.claim_calendar_publish_slot(
  p_row_id uuid, p_gym_id text, p_day date, p_timezone text,
  p_capacity integer, p_approved_only boolean
) returns boolean
language plpgsql security definer set search_path = public
as $$
declare
  v_row public.content_calendar%rowtype;
  v_used integer;
begin
  if p_capacity not between 1 and 2 or p_day is null or p_timezone is null then
    return false;
  end if;
  -- One lock per gym and actual publish day serializes all platform slots on
  -- that day. The row lock below still provides exactly-once for an individual id.
  perform pg_advisory_xact_lock(hashtextextended(p_gym_id || '|' || p_day::text, 0));
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
    where gym_id = p_gym_id and account = v_row.account and format = v_row.format
      and status in ('publishing', 'published')
      and (publish_reservation_day = p_day
           or (status = 'published' and published_at is not null
               and (published_at at time zone p_timezone)::date = p_day));
  if v_used >= p_capacity then
    return false;
  end if;
  update public.content_calendar
    set status = 'publishing', publish_reservation_day = p_day
    where id = p_row_id;
  return true;
end;
$$;

revoke all on function public.claim_calendar_publish_slot(uuid, text, date, text, integer, boolean)
  from public, anon, authenticated;
grant execute on function public.claim_calendar_publish_slot(uuid, text, date, text, integer, boolean)
  to service_role;
