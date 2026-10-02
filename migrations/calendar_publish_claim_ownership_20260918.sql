-- Apply before deploying the owned-claim publisher. The new code calls this RPC
-- exclusively and holds rows if it is unavailable. Keep the previous boolean
-- RPC for older workers during rollout; do not deploy code before this migration.
alter table public.content_calendar
  add column if not exists publish_claim_token uuid;

create or replace function public.claim_calendar_publish_slot_owned(
  p_row_id uuid, p_gym_id text, p_day date, p_timezone text,
  p_capacity integer, p_approved_only boolean
) returns uuid
language plpgsql security definer set search_path = public
as $$
declare
  v_row public.content_calendar%rowtype;
  v_used integer;
  v_token uuid;
begin
  if p_capacity not between 1 and 2 or p_day is null or p_timezone is null
      or nullif(btrim(p_gym_id), '') is null then
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

  select count(*) into v_used from public.content_calendar
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
