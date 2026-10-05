-- Exact-CAS repair of an EXISTING pending LASSO Story. Requires the paired
-- Story backfill migration. No row is deleted, re-dated, approved or published.
-- The logical-post immutability trigger is preserved: mismatched IDs block.
begin;

create or replace function public.lasso_story_review_matches(
  p_feed_id uuid, p_story_url text
) returns boolean
language plpgsql security definer set search_path = public stable
as $$
declare
  f public.content_calendar%rowtype;
  v_account text;
begin
  select * into f from public.content_calendar
   where id = p_feed_id and gym_id = 'lasso'
     and lower(btrim(coalesce(format, 'feed'))) = 'feed'
     and coalesce(variant_status, 'active') = 'active';
  if not found then return false; end if;
  v_account := lower(btrim(coalesce(f.account, '')));
  if v_account not in ('instagram', 'facebook')
     or f.slot_index not in (0, 1, 2)
     or f.post_date not between date '2026-10-02' and date '2026-11-08'
     or p_story_url is null or p_story_url = f.image_url then
    return false;
  end if;
  return exists (
    select 1 from public.echo_infographic_artifacts a
     where a.tenant in ('lasso', case when v_account = 'instagram'
                                    then 'lasso_ig' else 'lasso_fb' end)
       and a.image_url = p_story_url
       and a.image_sha256 ~ '^[0-9a-f]{64}$'
       and a.evidence->>'grade_status' = 'PASS'
       and a.evidence->>'image_sha256' = a.image_sha256
       and nullif(a.evidence->>'policy_version', '') is not null
       and a.evidence->>'aspect' = '9:16'
       and (
         (a.tenant = 'lasso'
          and a.source_identity->>'source_id' =
              'content_calendar:' || f.id::text || ':paired_story'
          and a.source_identity->>'source_feed_id' = f.id::text
          and a.source_identity->>'source_account' = v_account
          and a.source_identity->>'source_day' = f.post_date::text
          and a.source_identity->>'source_slot' = f.slot_index::text
          and a.source_identity->>'source_feed_caption' = f.caption
          and a.source_identity->>'source_feed_image_url' = f.image_url
          and a.source_identity->>'source_logical_post_id' =
              coalesce(f.logical_post_id::text, '')
          and a.source_identity->>'source_hash' ~ '^[0-9a-f]{64}$')
         or
         (a.tenant <> 'lasso'
          and a.source_identity->>'source_id' =
              'content_calendar:' || f.id::text || ':caption'
          and a.source_identity->>'source_hash' =
              encode(sha256(convert_to(f.caption, 'UTF8')), 'hex'))
       )
  );
end;
$$;

create or replace function public.lasso_story_current_source(p_story_id uuid)
returns boolean
language plpgsql security definer set search_path = public stable
as $$
declare
  s public.content_calendar%rowtype;
  f public.content_calendar%rowtype;
begin
  select * into s from public.content_calendar
   where id = p_story_id and gym_id = 'lasso'
     and lower(btrim(coalesce(format, 'feed'))) = 'story'
     and coalesce(variant_status, 'active') = 'active';
  if not found or s.slot_index not in (0, 1, 2) then return false; end if;
  if (
    select count(*) from public.content_calendar c
     where c.gym_id = 'lasso' and c.post_date = s.post_date
       and lower(btrim(coalesce(c.account, ''))) =
           lower(btrim(coalesce(s.account, '')))
       and lower(btrim(coalesce(c.format, 'feed'))) = 'feed'
       and c.slot_index = s.slot_index
       and coalesce(c.variant_status, 'active') = 'active'
  ) <> 1 then return false; end if;
  select * into f from public.content_calendar c
   where c.gym_id = 'lasso' and c.post_date = s.post_date
     and lower(btrim(coalesce(c.account, ''))) =
         lower(btrim(coalesce(s.account, '')))
     and lower(btrim(coalesce(c.format, 'feed'))) = 'feed'
     and c.slot_index = s.slot_index
     and coalesce(c.variant_status, 'active') = 'active';
  if s.logical_post_id is distinct from f.logical_post_id
     or s.pillar is distinct from f.pillar
     or s.caption is distinct from ''
     or s.image_url is null or s.source_media_url is distinct from s.image_url
     or s.scheduled_at is null
     or (s.scheduled_at at time zone 'America/New_York')::date <> s.post_date
     or (f.scheduled_at is not null and
         s.scheduled_at <> f.scheduled_at + interval '15 minutes') then
    return false;
  end if;
  return public.lasso_story_review_matches(f.id, s.image_url);
end;
$$;

create or replace function public.repair_lasso_paired_story(
  p_story_id uuid, p_feed_id uuid,
  p_expected_story jsonb, p_expected_feed jsonb,
  p_story_image_url text, p_story_sha256 text,
  p_artifact_tenant text, p_source_hash text, p_policy_version text,
  p_story_scheduled_at timestamptz
) returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
  s public.content_calendar%rowtype;
  f public.content_calendar%rowtype;
  v_account text;
  v_reason text;
begin
  if p_story_id is null or p_feed_id is null or p_story_id = p_feed_id
     or jsonb_typeof(p_expected_story) <> 'object'
     or jsonb_typeof(p_expected_feed) <> 'object'
     or p_story_image_url !~ '^https://'
     or p_story_sha256 !~ '^[0-9a-f]{64}$'
     or p_source_hash !~ '^[0-9a-f]{64}$'
     or p_artifact_tenant not in ('lasso', 'lasso_ig', 'lasso_fb')
     or nullif(p_policy_version, '') is null
     or p_story_scheduled_at is null then
    return jsonb_build_object('result','conflict','reason','invalid_input');
  end if;
  select * into s from public.content_calendar
   where id = p_story_id and gym_id = 'lasso'
     and lower(btrim(coalesce(format, 'feed'))) = 'story'
   for update;
  if not found then
    return jsonb_build_object('result','conflict','reason','story_missing');
  end if;
  v_account := lower(btrim(coalesce(s.account, '')));
  if v_account not in ('instagram','facebook')
     or s.post_date not between date '2026-10-02' and date '2026-11-08'
     or s.slot_index not in (0, 1, 2)
     or s.status <> 'pending' or s.variant_status <> 'active'
     or s.published_at is not null or s.late_post_id is not null
     or s.publish_claim_token is not null
     or s.media_not_ready_reason not in ('paired_feed_not_ready')
         and s.media_not_ready_reason is not null
     or s.caption is distinct from p_expected_story->>'caption'
     or s.pillar is distinct from p_expected_story->>'pillar'
     or s.image_url is distinct from p_expected_story->>'image_url'
     or s.source_media_url is distinct from p_expected_story->>'source_media_url'
     or s.media_not_ready_reason is distinct from
        p_expected_story->>'media_not_ready_reason'
     or s.scheduled_at is distinct from
        nullif(p_expected_story->>'scheduled_at','')::timestamptz
     or s.logical_post_id is distinct from
        nullif(p_expected_story->>'logical_post_id','')::uuid
     or s.status is distinct from p_expected_story->>'status'
     or s.variant_status is distinct from p_expected_story->>'variant_status'
     or s.post_date::text is distinct from p_expected_story->>'post_date'
     or s.slot_index::text is distinct from p_expected_story->>'slot_index'
     or v_account is distinct from lower(btrim(p_expected_story->>'account')) then
    return jsonb_build_object('result','conflict','reason','story_changed_or_claimed');
  end if;
  perform pg_advisory_xact_lock(hashtextextended(
    'lasso|' || v_account || '|' || s.post_date::text, 0));
  if (
    select count(*) from public.content_calendar c
     where c.gym_id = 'lasso' and c.post_date = s.post_date
       and lower(btrim(coalesce(c.account, ''))) = v_account
       and lower(btrim(coalesce(c.format, 'feed'))) = 'feed'
       and c.slot_index = s.slot_index and c.variant_status = 'active'
  ) <> 1 then
    return jsonb_build_object('result','conflict','reason','ambiguous_feed_slot');
  end if;
  select * into f from public.content_calendar
   where id = p_feed_id and gym_id = 'lasso'
     and lower(btrim(coalesce(account, ''))) = v_account
     and post_date = s.post_date and slot_index = s.slot_index
     and lower(btrim(coalesce(format, 'feed'))) = 'feed'
     and variant_status = 'active' for update;
  if not found or f.status not in ('pending','approved','published')
     or f.caption is distinct from p_expected_feed->>'caption'
     or f.image_url is distinct from p_expected_feed->>'image_url'
     or f.pillar is distinct from p_expected_feed->>'pillar'
     or f.status is distinct from p_expected_feed->>'status'
     or f.scheduled_at is distinct from
        nullif(p_expected_feed->>'scheduled_at','')::timestamptz
     or f.logical_post_id is distinct from
        nullif(p_expected_feed->>'logical_post_id','')::uuid
     or f.media_not_ready_reason is not null
     or (f.status = 'published' and
         (f.published_at is null or f.late_post_id is null))
     or (f.status in ('pending','approved') and
         (f.published_at is not null or f.late_post_id is not null))
     or s.logical_post_id is distinct from f.logical_post_id
     or p_story_image_url = f.image_url
     or (p_story_scheduled_at at time zone 'America/New_York')::date <> s.post_date
     or (f.scheduled_at is not null and
         p_story_scheduled_at <> f.scheduled_at + interval '15 minutes') then
    return jsonb_build_object('result','conflict','reason','feed_changed_or_pairing_mismatch');
  end if;
  if p_artifact_tenant = 'lasso_ig' and v_account <> 'instagram'
     or p_artifact_tenant = 'lasso_fb' and v_account <> 'facebook'
     or not exists (
       select 1 from public.echo_infographic_artifacts a
        where a.tenant = p_artifact_tenant
          and a.image_url = p_story_image_url
          and a.image_sha256 = p_story_sha256
          and a.evidence->>'grade_status' = 'PASS'
          and a.evidence->>'image_sha256' = p_story_sha256
          and a.evidence->>'policy_version' = p_policy_version
          and a.evidence->>'aspect' = '9:16'
          and a.source_identity->>'source_hash' = p_source_hash
     ) or not public.lasso_story_review_matches(f.id, p_story_image_url) then
    return jsonb_build_object('result','conflict','reason','reviewed_source_mismatch');
  end if;
  if exists (
    select 1 from public.content_calendar c where c.id <> s.id
      and c.gym_id = 'lasso'
      and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
      and c.variant_status = 'active'
      and c.status in ('pending','approved','publishing')
      and c.image_url = p_story_image_url
  ) then
    return jsonb_build_object('result','conflict','reason','story_media_already_in_use');
  end if;
  if exists (
    select 1 from public.content_calendar c where c.id <> s.id
      and c.gym_id = 'lasso' and c.post_date = s.post_date
      and lower(btrim(coalesce(c.account, ''))) = v_account
      and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
      and c.slot_index = s.slot_index
      and coalesce(c.variant_status, 'active') = 'active'
      and c.status = 'published' and c.published_at is not null
      and c.late_post_id is not null
  ) then
    return jsonb_build_object('result','conflict','reason','historical_story_already_published');
  end if;
  v_reason := case when f.status = 'published' then null
                   else 'paired_feed_not_ready' end;
  update public.content_calendar
     set pillar = f.pillar, caption = '', image_url = p_story_image_url,
         source_media_url = p_story_image_url,
         scheduled_at = p_story_scheduled_at,
         logical_post_id = f.logical_post_id,
         media_not_ready_reason = v_reason
   where id = s.id;
  if not public.lasso_story_current_source(s.id) then
    raise exception 'repaired Story failed source verification';
  end if;
  insert into public.lasso_managed_paired_stories(story_id, feed_id)
  values (s.id, f.id)
  on conflict (story_id) do nothing;
  if not exists (select 1 from public.lasso_managed_paired_stories m
                 where m.story_id = s.id and m.feed_id = f.id) then
    raise exception 'managed Story bound to another feed';
  end if;
  return jsonb_build_object('result','repaired','id',s.id,'feed_id',f.id,
    'hold_reason',v_reason);
end;
$$;

-- Called by the publisher's prefetch pass after the feed has a real receipt.
create or replace function public.release_lasso_paired_story_hold(p_story_id uuid)
returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
  s public.content_calendar%rowtype;
  f public.content_calendar%rowtype;
begin
  select * into s from public.content_calendar
   where id = p_story_id and gym_id = 'lasso'
     and lower(btrim(coalesce(format, 'feed'))) = 'story'
     and status = 'pending' and variant_status = 'active'
     and media_not_ready_reason = 'paired_feed_not_ready'
     and published_at is null and late_post_id is null
     and publish_claim_token is null for update;
  if not found then
    return jsonb_build_object('result','conflict','reason','not_releasable');
  end if;
  if not exists (select 1 from public.lasso_managed_paired_stories m
                 where m.story_id = s.id) then
    return jsonb_build_object('result','conflict','reason','unmanaged_story');
  end if;
  perform pg_advisory_xact_lock(hashtextextended(
    'lasso|' || lower(btrim(s.account)) || '|' || s.post_date::text, 0));
  if not public.lasso_story_current_source(s.id) then
    return jsonb_build_object('result','conflict','reason','source_changed');
  end if;
  select * into f from public.content_calendar
   where gym_id = 'lasso' and post_date = s.post_date
     and lower(btrim(coalesce(account, ''))) = lower(btrim(s.account))
     and lower(btrim(coalesce(format, 'feed'))) = 'feed'
     and slot_index = s.slot_index and variant_status = 'active';
  if not found or f.status <> 'published' or f.published_at is null
     or f.late_post_id is null then
    return jsonb_build_object('result','held','reason','paired_feed_not_ready');
  end if;
  if exists (
    select 1 from public.content_calendar c where c.id <> s.id
      and c.gym_id = 'lasso' and c.post_date = s.post_date
      and lower(btrim(coalesce(c.account, ''))) = lower(btrim(s.account))
      and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
      and c.slot_index = s.slot_index
      and coalesce(c.variant_status, 'active') = 'active'
      and c.status = 'published' and c.published_at is not null
      and c.late_post_id is not null
  ) then
    return jsonb_build_object('result','conflict','reason','historical_story_already_published');
  end if;
  update public.content_calendar set media_not_ready_reason = null
   where id = s.id;
  return jsonb_build_object('result','released','id',s.id,'feed_id',f.id);
end;
$$;

-- Enforce exact media/source pairing at the database claim boundary. An old
-- Story lacking proof cannot become publishing even if a stale worker omits
-- the application preflight. Published rows are never modified here.
create or replace function public.lasso_story_publish_source_guard()
returns trigger language plpgsql security definer set search_path = public
as $$
declare
  f public.content_calendar%rowtype;
begin
  if new.gym_id <> 'lasso'
     or lower(btrim(coalesce(new.format, 'feed'))) <> 'story'
     or new.post_date not between date '2026-10-02' and date '2026-11-08'
     or new.status <> 'publishing' or old.status = 'publishing' then
    return new;
  end if;
  -- Do not take over PR293's special Summit source path or preexisting legacy
  -- Stories. They enter this guard only after an exact reviewed repair.
  if not exists (select 1 from public.lasso_managed_paired_stories m
                 where m.story_id = old.id) then
    return new;
  end if;
  if not public.lasso_story_current_source(old.id)
     or old.media_not_ready_reason is not null
     or new.media_not_ready_reason is not null then
    raise exception 'LASSO Story source proof missing or held'
      using errcode = '23514';
  end if;
  select * into f from public.content_calendar
   where gym_id = 'lasso' and post_date = old.post_date
     and lower(btrim(coalesce(account, ''))) = lower(btrim(old.account))
     and lower(btrim(coalesce(format, 'feed'))) = 'feed'
     and slot_index = old.slot_index and variant_status = 'active';
  if not found or f.status <> 'published' or f.published_at is null
     or f.late_post_id is null then
    raise exception 'LASSO paired feed has no publish receipt'
      using errcode = '23514';
  end if;
  if not exists (select 1 from public.lasso_managed_paired_stories m
                 where m.story_id = old.id and m.feed_id = f.id) then
    raise exception 'LASSO managed Story feed identity changed'
      using errcode = '23514';
  end if;
  if exists (
    select 1 from public.content_calendar c where c.id <> old.id
      and c.gym_id = 'lasso' and c.post_date = old.post_date
      and lower(btrim(coalesce(c.account, ''))) = lower(btrim(old.account))
      and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
      and c.slot_index = old.slot_index
      and coalesce(c.variant_status, 'active') = 'active'
      and c.status = 'published' and c.published_at is not null
      and c.late_post_id is not null
  ) then
    raise exception 'LASSO Story already published in paired slot'
      using errcode = '23514';
  end if;
  return new;
end;
$$;
drop trigger if exists lasso_story_publish_source_guard on public.content_calendar;
create trigger lasso_story_publish_source_guard
  before update of status on public.content_calendar
  for each row execute function public.lasso_story_publish_source_guard();

-- The daily generator probes this before any paid render. Code deployed ahead
-- of both migrations therefore stays inert until the managed claim guard is
-- installed as well as the stage RPC.
create or replace function public.lasso_paired_story_system_ready()
returns boolean language sql security definer set search_path = public stable
as $$ select true $$;

revoke all on function public.lasso_story_review_matches(uuid,text)
  from public, anon, authenticated;
revoke all on function public.lasso_story_current_source(uuid)
  from public, anon, authenticated;
revoke all on function public.repair_lasso_paired_story(
  uuid,uuid,jsonb,jsonb,text,text,text,text,text,timestamptz)
  from public, anon, authenticated;
revoke all on function public.release_lasso_paired_story_hold(uuid)
  from public, anon, authenticated;
grant execute on function public.lasso_story_review_matches(uuid,text)
  to service_role;
grant execute on function public.lasso_story_current_source(uuid)
  to service_role;
grant execute on function public.repair_lasso_paired_story(
  uuid,uuid,jsonb,jsonb,text,text,text,text,text,timestamptz)
  to service_role;
grant execute on function public.release_lasso_paired_story_hold(uuid)
  to service_role;
revoke all on function public.lasso_paired_story_system_ready()
  from public, anon, authenticated;
grant execute on function public.lasso_paired_story_system_ready()
  to service_role;
commit;
