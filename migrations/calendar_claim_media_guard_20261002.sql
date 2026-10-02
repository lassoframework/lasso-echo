-- Needs-media holds must never be claimable by the atomic publish-slot RPC.
--
-- A Story slot whose reviewed 9:16 render or hosting failed is staged as a
-- durable hold: status 'pending' (mapped from a BLOCKED draft), blank image_url
-- and a media_not_ready_reason. due_rows and mark_publishing already refuse such
-- rows, but claim_calendar_publish_slot_owned is a separate atomic claim path
-- and previously only checked status/published_at/variant_status -- a stale
-- 'pending' or 'approved' hold row could be claimed and published with no media.
--
-- This redefinition of publish_capacity_current_day_20260930.sql adds two
-- fail-closed guards to the row selection: image_url must be a non-blank string
-- and media_not_ready_reason must be NULL. Every other guard (owned claim,
-- advisory lock, capacity, format, approved-only) is preserved verbatim.
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
      -- media hold guards: never claim a row without real, ready media
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
  return v_token;
end;
$$;

revoke all on function public.claim_calendar_publish_slot_owned(uuid, text, date, text, integer, boolean)
  from public, anon, authenticated;
grant execute on function public.claim_calendar_publish_slot_owned(uuid, text, date, text, integer, boolean)
  to service_role;

-- The portal's pre-read is useful for human-facing errors, but media can change
-- between that read and an approval PATCH. Approve only a still-pending row with
-- real media in the same UPDATE statement. A missing/blank image or needs-media
-- reason yields no row and leaves the current status untouched.
create or replace function public.approve_calendar_row_if_media_ready(
  p_row_id uuid, p_gym_id text
) returns setof public.content_calendar
language sql security definer set search_path = public
as $$
  update public.content_calendar
     set status = 'approved'
   where id = p_row_id and gym_id = p_gym_id
     and status = 'pending' and published_at is null
     and late_post_id is null
     and nullif(btrim(coalesce(image_url, '')), '') is not null
     and media_not_ready_reason is null
  returning *;
$$;

revoke all on function public.approve_calendar_row_if_media_ready(uuid, text)
  from public, anon, authenticated;
grant execute on function public.approve_calendar_row_if_media_ready(uuid, text)
  to service_role;
