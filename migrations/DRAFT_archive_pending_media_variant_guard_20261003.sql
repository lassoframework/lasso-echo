-- Archived content_calendar variants are historical audit records. They may retain
-- pending status and media for forensics, but they must never be approved or claimed.
-- This migration only tightens the existing approval RPC; it performs no archive.
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
     and variant_status = 'active'
     and nullif(btrim(coalesce(image_url, '')), '') is not null
     and media_not_ready_reason is null
  returning *;
$$;

revoke all on function public.approve_calendar_row_if_media_ready(uuid, text)
  from public, anon, authenticated;
grant execute on function public.approve_calendar_row_if_media_ready(uuid, text)
  to service_role;
