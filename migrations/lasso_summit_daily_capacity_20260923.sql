-- LASSO Summit daily extra (Blake's explicit ruling, 2026-09-23): allow a THIRD
-- same-day feed reservation ONLY for the canonical gym_id 'lasso' and ONLY for
-- reservation days inside the dated Nashville Growth Summit window
-- 2026-09-23..2026-11-08 inclusive (two regular feed posts + one extra Summit
-- post; the Summit post never replaces a regular slot). Every other gym keeps
-- the 1..2 capacity bound — this is NOT a global 3/day expansion.
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
      and post_date between date '2026-09-23' and date '2026-11-08'
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
