-- Durable LASSO three-feed cadence (2026-09-30): allow a THIRD same-day feed
-- reservation only for canonical gym_id 'lasso'. Client gyms keep the 1..2
-- capacity bound. This replaces the dated Summit-only capacity envelope; the
-- application flag AGENT_LASSO_3X_ENABLED controls whether LASSO requests 3.
--
-- Replacement for calendar_publish_claim_ownership_20260918.sql's owned-claim
-- RPC. The signature, SECURITY DEFINER posture, search_path pin, per-gym
-- advisory lock, pending/approved status gate, published_at/late_post_id guards,
-- variant_status gate, account+format day accounting, publish_reservation_day
-- stamping, ownership token, and grants are all preserved byte-for-byte; the
-- only change is the capacity precondition. Apply before deploying worker code
-- that resolves capacity 3; the worker holds rows if the RPC rejects them, it
-- never falls back to an unsafe split read.
alter table public.content_calendar
  drop constraint if exists content_calendar_slot_index_check;
alter table public.content_calendar
  add constraint content_calendar_slot_index_check check (
    slot_index is null or slot_index in (0, 1) or (
      slot_index = 2
      and gym_id = 'lasso'
      and coalesce(nullif(lower(btrim(format)), ''), 'feed') = 'feed'
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
  v_token uuid;
begin
  if p_capacity < 1 or p_capacity > 3 or p_day is null or p_timezone is null
      or nullif(btrim(p_gym_id), '') is null then
    return null;
  end if;
  -- Capacity 3 is LASSO-only. Client gyms can never widen past 2.
  if p_capacity = 3 and p_gym_id <> 'lasso' then
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
