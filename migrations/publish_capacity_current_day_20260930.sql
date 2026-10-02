-- Scope in-flight capacity to the actual reservation day for every tenant.
--
-- A stale claim from a prior local day is still reconciled separately, but it
-- must not consume today's cadence forever.  The previous RPC counted every
-- row in status='publishing' regardless of publish_reservation_day; one old
-- ambiguous claim could therefore stop a 1x client indefinitely, and two old
-- claims could stop a 2x client.  Current-day ambiguous claims still reserve
-- capacity, preventing same-day floods and duplicates.
--
-- This supersedes lasso_three_feed_capacity_20260930.sql while preserving its
-- LASSO-only capacity-3 rule and every owned-claim/CAS guard.
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
  if p_capacity < 1 or p_capacity > 3 or p_day is null or p_timezone is null
      or nullif(btrim(p_gym_id), '') is null then
    return null;
  end if;
  if p_capacity = 3 and p_gym_id <> 'lasso' then
    return null;
  end if;

  perform pg_advisory_xact_lock(hashtextextended(p_gym_id, 0));
  select * into v_row from public.content_calendar
    where id = p_row_id and gym_id = p_gym_id
      and status in ('pending', 'approved') and published_at is null
      and late_post_id is null and variant_status = 'active'
    for update;
  if not found or (p_approved_only and v_row.status <> 'approved') then
    return null;
  end if;
  if p_capacity = 3 and
      coalesce(nullif(lower(btrim(v_row.format)), ''), 'feed') <> 'feed' then
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
    where id = p_row_id;
  return v_token;
end;
$$;

revoke all on function public.claim_calendar_publish_slot_owned(uuid, text, date, text, integer, boolean)
  from public, anon, authenticated;
grant execute on function public.claim_calendar_publish_slot_owned(uuid, text, date, text, integer, boolean)
  to service_role;
