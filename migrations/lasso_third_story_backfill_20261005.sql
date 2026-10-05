-- Insert one reviewed LASSO Instagram Story for an existing third-slot feed.
-- Requires lasso_three_story_capacity_20261005.sql and the logical_post_id
-- column. This function never changes an existing calendar row.
-- Two provenance paths, both fail-closed:
--   * p_caption_hash IS NULL: catalog parity; the reviewed feed artifact and
--     the Story artifact bind the approved catalog source hash (legacy arity
--     and behavior for existing callers).
--   * p_caption_hash IS NOT NULL: LIVE-feed path; provenance binds the exact
--     feed caption hash instead. This is NOT catalog parity; the caller must
--     already have validated Summit window + approved facts in Python.
begin;
-- The slot index is unique even for writers outside this RPC. A preflight
-- query found zero active LASSO Instagram Story slot-2 rows before rollout.
create unique index if not exists lasso_active_ig_story_slot2_unique
  on public.content_calendar
    (gym_id, post_date, lower(btrim(account)), lower(btrim(format)), slot_index)
  where gym_id = 'lasso' and slot_index = 2 and variant_status = 'active'
    and lower(btrim(coalesce(account, ''))) = 'instagram'
    and lower(btrim(coalesce(format, 'feed'))) = 'story'
    and status not in ('denied', 'killed', 'failed');

create or replace function public.stage_lasso_third_story(
  p_feed_id uuid, p_feed_caption text, p_feed_image_url text,
  p_story_id uuid, p_story_image_url text, p_story_source_url text,
  p_story_sha256 text, p_source_hash text, p_policy_version text,
  p_scheduled_at timestamptz,
  p_caption_hash text default null
) returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
  v_feed public.content_calendar%rowtype;
  v_existing public.content_calendar%rowtype;
  v_day date;
begin
  if p_feed_id is null or p_story_id is null or p_feed_id = p_story_id
     or nullif(btrim(coalesce(p_feed_caption, '')), '') is null
     or nullif(btrim(coalesce(p_feed_image_url, '')), '') is null
     or nullif(btrim(coalesce(p_story_image_url, '')), '') is null
     or nullif(btrim(coalesce(p_story_source_url, '')), '') is null
     or p_story_image_url = p_feed_image_url
     or p_story_source_url <> p_story_image_url
     or p_story_sha256 is null or p_source_hash is null
     or p_story_sha256 !~ '^[0-9a-f]{64}$'
     or p_source_hash !~ '^[0-9a-f]{64}$'
     or (p_caption_hash is not null and p_caption_hash !~ '^[0-9a-f]{64}$')
     or (p_caption_hash is not null and p_source_hash <> p_caption_hash)
     or nullif(btrim(coalesce(p_policy_version, '')), '') is null
     or p_scheduled_at is null then
    return jsonb_build_object('result','conflict','reason','invalid_input');
  end if;

  -- Also conflicts with ordinary INSERT/UPDATE/DELETE writers. The advisory
  -- lock provides the per-gym/day coordination contract for this RPC.
  lock table public.content_calendar in share row exclusive mode;
  select * into v_feed from public.content_calendar
   where id = p_feed_id and gym_id = 'lasso' and lower(btrim(account)) = 'instagram'
     and lower(btrim(coalesce(format, 'feed'))) = 'feed'
     and slot_index = 2 and variant_status = 'active'
   for update;
  if not found then
    return jsonb_build_object('result','conflict','reason','source_feed_missing');
  end if;
  v_day := v_feed.post_date;
  perform pg_advisory_xact_lock(hashtextextended('lasso|instagram|' || v_day::text, 0));
  if (p_caption_hash is null and v_day not between date '2026-10-01' and date '2026-11-08')
     or (p_caption_hash is not null and v_day not between date '2026-09-23' and date '2026-11-08')
     or v_feed.status not in ('pending','approved','publishing','published')
     or v_feed.caption is distinct from p_feed_caption
     or v_feed.image_url is distinct from p_feed_image_url
     or lower(btrim(coalesce(v_feed.pillar, ''))) <> 'summit'
     or (p_scheduled_at at time zone 'America/New_York')::date <> v_day then
    return jsonb_build_object('result','conflict','reason','source_feed_changed_or_out_of_scope');
  end if;
  -- The reviewed feed artifact must carry the exact provenance hash: the
  -- approved catalog hash (catalog path) or the exact feed caption hash
  -- (LIVE-feed path).
  if not exists (
    select 1 from public.echo_infographic_artifacts a
     where a.tenant = 'lasso' and a.image_url = p_feed_image_url
       and a.source_identity->>'source_hash' = p_source_hash
       and (p_caption_hash is null or
            a.source_identity->>'source_id' =
              'content_calendar:' || p_feed_id::text || ':caption')
       and a.evidence->>'grade_status' = 'PASS'
       and a.evidence->>'image_sha256' = a.image_sha256
  ) then
    return jsonb_build_object('result','conflict','reason','reviewed_feed_source_mismatch');
  end if;

  -- A Story must have its own reviewed 9:16 delivered object whose recorded
  -- source is this exact feed row and the same exact provenance hash: the
  -- approved catalog hash (catalog path) or the exact feed caption hash
  -- (LIVE-feed path; a changed caption or URL invalidates the story).
  if not exists (
    select 1 from public.echo_infographic_artifacts a
     where a.tenant = 'lasso' and a.image_url = p_story_image_url
       and a.image_sha256 = p_story_sha256
       and a.evidence->>'grade_status' = 'PASS'
       and a.evidence->>'image_sha256' = p_story_sha256
       and a.evidence->>'policy_version' = p_policy_version
       and a.evidence->>'aspect' = '9:16'
       and a.source_identity->>'source_feed_id' = p_feed_id::text
       and a.source_identity->>'source_feed_image_url' = p_feed_image_url
       and a.source_identity->>'source_hash' = p_source_hash
  ) then
    return jsonb_build_object('result','conflict','reason','reviewed_9x16_source_mismatch');
  end if;

  select * into v_existing from public.content_calendar where id = p_story_id;
  if found then
    if v_existing.gym_id = 'lasso' and lower(btrim(v_existing.account)) = 'instagram'
       and v_existing.post_date = v_day and lower(btrim(v_existing.format)) = 'story'
       and v_existing.slot_index = 2 and v_existing.variant_status = 'active'
       and v_existing.caption = '' and v_existing.image_url = p_story_image_url
       and v_existing.source_media_url = p_story_source_url
       and v_existing.status in ('pending','approved','publishing','published')
       and v_existing.media_not_ready_reason is null
       and v_existing.logical_post_id is not distinct from v_feed.logical_post_id
       and v_existing.scheduled_at is not distinct from p_scheduled_at then
      return jsonb_build_object('result','idempotent','id',p_story_id);
    end if;
    return jsonb_build_object('result','conflict','reason','id_reused');
  end if;

  -- A legacy NULL ordinal or an already held Story also owns the logical slot.
  if exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso' and c.post_date = v_day
       and lower(btrim(coalesce(c.account, ''))) = 'instagram'
       and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
       and c.variant_status = 'active'
       and c.status not in ('denied','killed','failed')
       and (c.slot_index = 2 or c.slot_index is null)
  ) then
    return jsonb_build_object('result','conflict','reason','occupied_story_slot');
  end if;
  if exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso' and lower(btrim(coalesce(c.account, ''))) = 'instagram'
       and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
       and c.variant_status = 'active'
       and c.status not in ('denied','killed','failed')
       and c.image_url = p_story_image_url
  ) then
    return jsonb_build_object('result','conflict','reason','story_media_already_in_use');
  end if;

  insert into public.content_calendar
    (id,gym_id,account,post_date,pillar,format,caption,image_url,
     source_media_url,status,scheduled_at,slot_index,variant_status,logical_post_id)
  values
    (p_story_id,'lasso','instagram',v_day,'summit','story','',p_story_image_url,
     p_story_source_url,'pending',p_scheduled_at,2,'active',v_feed.logical_post_id);
  return jsonb_build_object('result','inserted','id',p_story_id,'feed_id',p_feed_id,
                            'logical_post_id',v_feed.logical_post_id);
exception when unique_violation then
  return jsonb_build_object('result','conflict','reason','unique_violation');
end;
$$;
revoke all on function public.stage_lasso_third_story(
  uuid,text,text,uuid,text,text,text,text,text,timestamptz,text)
  from public,anon,authenticated;
grant execute on function public.stage_lasso_third_story(
  uuid,text,text,uuid,text,text,text,text,text,timestamptz,text)
  to service_role;
commit;
