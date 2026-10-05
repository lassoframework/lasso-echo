-- Release only the October 2–5 LASSO prepared-backlog feed hold after its
-- exact managed, reviewed, pending Story has committed. No provider action.
-- Requires lasso_paired_story_backfill and lasso_paired_story_repair migrations.
begin;

create or replace function public.release_lasso_backlog_feed_hold(
  p_feed_id uuid, p_story_id uuid, p_expected_feed jsonb,
  p_expected_story jsonb
) returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
  f public.content_calendar%rowtype;
  s public.content_calendar%rowtype;
  v_account text;
  v_day date;
  v_count integer;
begin
  if p_feed_id is null or p_story_id is null or p_feed_id = p_story_id
     or jsonb_typeof(p_expected_feed) <> 'object'
     or jsonb_typeof(p_expected_story) <> 'object'
     or not (p_expected_feed ?& array['id','gym_id','account','post_date',
       'slot_index','format','status','variant_status','caption','image_url',
       'pillar','scheduled_at','logical_post_id','media_not_ready_reason',
       'published_at','late_post_id','publish_claim_token'])
     or not (p_expected_story ?& array['id','gym_id','account','post_date',
       'slot_index','format','status','variant_status','caption','image_url',
       'source_media_url','pillar','scheduled_at','logical_post_id',
       'media_not_ready_reason','published_at','late_post_id',
       'publish_claim_token'])
     or p_expected_feed->>'id' is distinct from p_feed_id::text
     or p_expected_story->>'id' is distinct from p_story_id::text
     or p_expected_feed->>'media_not_ready_reason' is distinct from
        'prepared_backlog_waiting_for_story_and_capacity' then
    return jsonb_build_object('result','conflict','reason','invalid_input');
  end if;
  v_account := lower(btrim(coalesce(p_expected_feed->>'account','')));
  if v_account not in ('instagram','facebook')
     or p_expected_feed->>'gym_id' <> 'lasso'
     or p_expected_story->>'gym_id' <> 'lasso'
     or p_expected_feed->>'post_date' !~ '^2026-10-0[2-5]$'
     or p_expected_feed->>'slot_index' not in ('0','1','2')
     or p_expected_feed->>'status' <> 'pending'
     or p_expected_story->>'status' <> 'pending' then
    return jsonb_build_object('result','conflict','reason','invalid_scope');
  end if;
  v_day := (p_expected_feed->>'post_date')::date;
  perform pg_advisory_xact_lock(hashtextextended(
    'lasso|' || v_account || '|' || v_day::text, 0));
  select * into f from public.content_calendar where id = p_feed_id for update;
  if not found or f.gym_id <> 'lasso' or f.post_date <> v_day
     or lower(btrim(coalesce(f.account,''))) <> v_account
     or lower(btrim(coalesce(f.format,'feed'))) <> 'feed'
     or f.slot_index not in (0,1,2)
     or f.status <> 'pending' or f.variant_status <> 'active'
     or f.published_at is not null or f.late_post_id is not null
     or f.publish_claim_token is not null
     or f.publish_reservation_day is not null
     or nullif(btrim(coalesce(f.caption,'')),'') is null
     or f.image_url !~ '^https://' then
    return jsonb_build_object('result','conflict','reason','feed_unready');
  end if;
  if f.id::text is distinct from p_expected_feed->>'id'
     or f.gym_id is distinct from p_expected_feed->>'gym_id'
     or f.account is distinct from p_expected_feed->>'account'
     or f.post_date::text is distinct from p_expected_feed->>'post_date'
     or f.slot_index::text is distinct from p_expected_feed->>'slot_index'
     or f.format is distinct from p_expected_feed->>'format'
     or f.status is distinct from p_expected_feed->>'status'
     or f.variant_status is distinct from p_expected_feed->>'variant_status'
     or f.caption is distinct from p_expected_feed->>'caption'
     or f.image_url is distinct from p_expected_feed->>'image_url'
     or f.pillar is distinct from p_expected_feed->>'pillar'
     or f.scheduled_at is distinct from
        nullif(p_expected_feed->>'scheduled_at','')::timestamptz
     or f.logical_post_id is distinct from
        nullif(p_expected_feed->>'logical_post_id','')::uuid
     or f.published_at is distinct from
        nullif(p_expected_feed->>'published_at','')::timestamptz
     or f.late_post_id::text is distinct from p_expected_feed->>'late_post_id'
     or f.publish_claim_token is distinct from
        nullif(p_expected_feed->>'publish_claim_token','')::uuid then
    return jsonb_build_object('result','conflict','reason','feed_changed');
  end if;
  if f.media_not_ready_reason is distinct from
       'prepared_backlog_waiting_for_story_and_capacity'
     and f.media_not_ready_reason is not null then
    return jsonb_build_object('result','conflict','reason','feed_hold_changed');
  end if;
  select count(*) into v_count from public.content_calendar c
   where c.gym_id = 'lasso' and c.post_date = v_day
     and lower(btrim(coalesce(c.account,''))) = v_account
     and lower(btrim(coalesce(c.format,'feed'))) = 'feed'
     and c.slot_index = f.slot_index
     and coalesce(c.variant_status,'active') = 'active';
  if v_count <> 1 then
    return jsonb_build_object('result','conflict','reason','ambiguous_feed_slot');
  end if;
  select * into s from public.content_calendar where id = p_story_id for update;
  if not found or s.gym_id <> 'lasso' or s.post_date <> v_day
     or lower(btrim(coalesce(s.account,''))) <> v_account
     or lower(btrim(coalesce(s.format,'feed'))) <> 'story'
     or s.slot_index is distinct from f.slot_index
     or s.status <> 'pending' or s.variant_status <> 'active'
     or s.published_at is not null or s.late_post_id is not null
     or s.publish_claim_token is not null
     or s.publish_reservation_day is not null
     or s.media_not_ready_reason not in ('paired_feed_not_ready')
        and s.media_not_ready_reason is not null
     or s.image_url !~ '^https://' or s.image_url = f.image_url
     or s.source_media_url is distinct from s.image_url then
    return jsonb_build_object('result','conflict','reason','story_unready');
  end if;
  if s.id::text is distinct from p_expected_story->>'id'
     or s.gym_id is distinct from p_expected_story->>'gym_id'
     or s.account is distinct from p_expected_story->>'account'
     or s.post_date::text is distinct from p_expected_story->>'post_date'
     or s.slot_index::text is distinct from p_expected_story->>'slot_index'
     or s.format is distinct from p_expected_story->>'format'
     or s.status is distinct from p_expected_story->>'status'
     or s.variant_status is distinct from p_expected_story->>'variant_status'
     or s.caption is distinct from p_expected_story->>'caption'
     or s.image_url is distinct from p_expected_story->>'image_url'
     or s.source_media_url is distinct from p_expected_story->>'source_media_url'
     or s.pillar is distinct from p_expected_story->>'pillar'
     or s.scheduled_at is distinct from
        nullif(p_expected_story->>'scheduled_at','')::timestamptz
     or s.logical_post_id is distinct from
        nullif(p_expected_story->>'logical_post_id','')::uuid
     or s.media_not_ready_reason is distinct from
        p_expected_story->>'media_not_ready_reason'
     or s.published_at is distinct from
        nullif(p_expected_story->>'published_at','')::timestamptz
     or s.late_post_id::text is distinct from p_expected_story->>'late_post_id'
     or s.publish_claim_token is distinct from
        nullif(p_expected_story->>'publish_claim_token','')::uuid then
    return jsonb_build_object('result','conflict','reason','story_changed');
  end if;
  select count(*) into v_count from public.content_calendar c
   where c.gym_id = 'lasso' and c.post_date = v_day
     and lower(btrim(coalesce(c.account,''))) = v_account
     and lower(btrim(coalesce(c.format,'feed'))) = 'story'
     and c.slot_index = f.slot_index
     and coalesce(c.variant_status,'active') = 'active'
     and c.status not in ('denied','killed','failed');
  if v_count <> 1 or exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso' and c.post_date = v_day
       and lower(btrim(coalesce(c.account,''))) = v_account
       and lower(btrim(coalesce(c.format,'feed'))) = 'story'
       and c.slot_index is null
       and coalesce(c.variant_status,'active') = 'active'
       and c.status not in ('denied','killed','failed')) then
    return jsonb_build_object('result','conflict','reason','ambiguous_story_slot');
  end if;
  if not exists (select 1 from public.lasso_managed_paired_stories m
                  where m.story_id = s.id and m.feed_id = f.id)
     or not public.lasso_story_current_source(s.id) then
    return jsonb_build_object('result','conflict','reason','managed_story_source_unverified');
  end if;
  if f.media_not_ready_reason is null then
    return jsonb_build_object('result','idempotent','feed_id',f.id,'story_id',s.id);
  end if;
  update public.content_calendar
     set media_not_ready_reason = null
   where id = f.id and gym_id = 'lasso'
     and status = 'pending' and variant_status = 'active'
     and media_not_ready_reason =
       'prepared_backlog_waiting_for_story_and_capacity'
     and published_at is null and late_post_id is null
     and publish_claim_token is null and publish_reservation_day is null;
  if not found then
    raise exception 'LASSO backlog feed hold CAS lost';
  end if;
  if not public.lasso_story_current_source(s.id) then
    raise exception 'LASSO backlog Story proof changed during release';
  end if;
  return jsonb_build_object('result','released','feed_id',f.id,'story_id',s.id);
end;
$$;

revoke all on function public.release_lasso_backlog_feed_hold(uuid,uuid,jsonb,jsonb)
  from public, anon, authenticated;
grant execute on function public.release_lasso_backlog_feed_hold(uuid,uuid,jsonb,jsonb)
  to service_role;
commit;
