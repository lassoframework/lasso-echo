-- DRAFT / UNAPPLIED / OFF — DO NOT APPLY, DO NOT ACTIVATE.
--
-- Persisted-state repair for the two public calendar RPCs used by runtime.
-- This file is standalone from both unaccepted Kimi RPC drafts. Apply it after
-- calendar_claim_media_guard_20261002.sql and after the frozen scene-wave stack
-- that installs the authoritative BEFORE UPDATE trigger. No wrapper or prior
-- scene-RPC redefinition is a dependency. A pre-write predicate cannot observe
-- a trigger that converts an eligible approval or publish claim into the
-- durable scene-held state, so this file makes the real caller contracts read
-- the persisted row after that trigger finishes.
--
-- This additive redefinition preserves the original selection, tenant,
-- advisory-lock, capacity, approved-only, media, format and CAS behavior. The
-- only behavioral change is that success is decided from a second statement
-- that reads the row after every row trigger has finished:
--
--   * a claim returns its UUID only when the persisted row is publishing with
--     that exact token and requested reservation day;
--   * approval returns a row only when the persisted row is actually approved
--     and still satisfies the original media-ready gates.
--
-- A trigger-converted scene hold therefore commits normally while callers see
-- NULL / an empty set. No trigger, flag, scene/group function or frozen draft
-- is changed here. Rollback in a scratch database is re-applying
-- calendar_claim_media_guard_20261002.sql.

begin;

create or replace function public.claim_calendar_publish_slot_owned(
  p_row_id uuid, p_gym_id text, p_day date, p_timezone text,
  p_capacity integer, p_approved_only boolean
) returns uuid
language plpgsql security definer set search_path = public
as $$
declare
  v_row public.content_calendar%rowtype;
  v_persisted public.content_calendar%rowtype;
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
      and nullif(btrim(coalesce(image_url, '')), '') is not null
      and media_not_ready_reason is null
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

  -- A separate statement observes the row persisted after BEFORE/AFTER row
  -- triggers. The generated token alone is attempted state, not proof that the
  -- claim survived the authoritative scene guard.
  select * into v_persisted from public.content_calendar
    where id = p_row_id and gym_id = p_gym_id;
  if not found
      or v_persisted.status is distinct from 'publishing'
      or v_persisted.publish_claim_token is distinct from v_token
      or v_persisted.publish_reservation_day is distinct from p_day then
    return null;
  end if;
  return v_token;
end;
$$;

revoke all on function public.claim_calendar_publish_slot_owned(uuid, text, date, text, integer, boolean)
  from public, anon, authenticated;
grant execute on function public.claim_calendar_publish_slot_owned(uuid, text, date, text, integer, boolean)
  to service_role;

create or replace function public.approve_calendar_row_if_media_ready(
  p_row_id uuid, p_gym_id text
) returns setof public.content_calendar
language plpgsql security definer set search_path = public
as $$
declare
  v_persisted public.content_calendar%rowtype;
begin
  update public.content_calendar
     set status = 'approved'
   where id = p_row_id and gym_id = p_gym_id
     and status = 'pending' and published_at is null
     and late_post_id is null
     and nullif(btrim(coalesce(image_url, '')), '') is not null
     and media_not_ready_reason is null;
  if not found then
    return;
  end if;

  -- Read persisted state in a new statement. UPDATE ... RETURNING would expose
  -- a trigger-converted held row to SETOF callers as one successful result.
  select * into v_persisted from public.content_calendar
    where id = p_row_id and gym_id = p_gym_id;
  if not found
      or v_persisted.status is distinct from 'approved'
      or v_persisted.published_at is not null
      or v_persisted.late_post_id is not null
      or nullif(btrim(coalesce(v_persisted.image_url, '')), '') is null
      or v_persisted.media_not_ready_reason is not null then
    return;
  end if;

  return next v_persisted;
  return;
end;
$$;

revoke all on function public.approve_calendar_row_if_media_ready(uuid, text)
  from public, anon, authenticated;
grant execute on function public.approve_calendar_row_if_media_ready(uuid, text)
  to service_role;

commit;
