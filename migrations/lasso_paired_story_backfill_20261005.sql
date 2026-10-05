-- Add only missing, independently reviewed LASSO Stories for exact live feeds.
-- Prerequisites: content_calendar.source_media_url, logical_post_id, and the
-- lasso_three_story_capacity_20261005 slot/claim migration. Apply this migration
-- only after the active-slot uniqueness preflight has passed.
begin;

-- Only Stories created or exactly repaired by these RPCs use the new source
-- claim guard. Historical Summit and legacy Stories retain their existing
-- publisher path until they are individually reviewed and registered.
create table if not exists public.lasso_managed_paired_stories (
  story_id uuid primary key references public.content_calendar(id),
  feed_id uuid not null references public.content_calendar(id),
  created_at timestamptz not null default now()
);
revoke all on public.lasso_managed_paired_stories from public, anon, authenticated;
grant select on public.lasso_managed_paired_stories to service_role;

-- Existing ambiguous legacy NULL slots must be reconciled before this guard
-- is installed. The migration fails without changing calendar rows.
do $$
begin
  if exists (
    select 1 from public.content_calendar legacy
    join public.content_calendar numbered
      on numbered.gym_id = legacy.gym_id
     and numbered.post_date = legacy.post_date
     and lower(btrim(coalesce(numbered.account, ''))) =
         lower(btrim(coalesce(legacy.account, '')))
     and numbered.id <> legacy.id
    where legacy.gym_id = 'lasso'
      and lower(btrim(coalesce(legacy.format, 'feed'))) = 'story'
      and lower(btrim(coalesce(numbered.format, 'feed'))) = 'story'
      and coalesce(legacy.variant_status, 'active') = 'active'
      and coalesce(numbered.variant_status, 'active') = 'active'
      and coalesce(legacy.status, 'pending') in ('pending','approved','publishing')
      and coalesce(numbered.status, 'pending') in ('pending','approved','publishing')
      and legacy.slot_index is null
      and numbered.slot_index in (0, 1, 2)
  ) then
    raise exception 'LASSO active unnumbered Story conflicts with a numbered Story';
  end if;
end;
$$;

-- Unique indexes cover both numbered and legacy NULL slots for every writer.
create unique index if not exists lasso_active_story_account_day_slot_unique
  on public.content_calendar
    (gym_id, post_date, lower(btrim(account)), slot_index)
  where gym_id = 'lasso'
    and lower(btrim(coalesce(format, 'feed'))) = 'story'
    and coalesce(variant_status, 'active') = 'active'
    and coalesce(status, 'pending') in ('pending', 'approved', 'publishing')
    and slot_index in (0, 1, 2);
create unique index if not exists lasso_active_story_account_day_null_slot_unique
  on public.content_calendar
    (gym_id, post_date, lower(btrim(account)))
  where gym_id = 'lasso'
    and lower(btrim(coalesce(format, 'feed'))) = 'story'
    and coalesce(variant_status, 'active') = 'active'
    and coalesce(status, 'pending') in ('pending', 'approved', 'publishing')
    and slot_index is null;

-- A narrow trigger makes old NULL slots conflict with numbered slots under
-- the same per-account/day advisory key used by the insert RPC. It never
-- locks unrelated gyms or dates.
create or replace function public.lasso_story_legacy_slot_guard()
returns trigger language plpgsql security definer set search_path = public
as $$
declare
  v_account text;
begin
  if new.gym_id <> 'lasso'
     or lower(btrim(coalesce(new.format, 'feed'))) <> 'story'
     or coalesce(new.variant_status, 'active') <> 'active'
     or coalesce(new.status, 'pending') not in ('pending', 'approved', 'publishing')
     or new.slot_index not in (0, 1, 2) and new.slot_index is not null then
    return new;
  end if;
  v_account := lower(btrim(coalesce(new.account, '')));
  if v_account not in ('instagram', 'facebook') or new.post_date is null then
    return new;
  end if;
  perform pg_advisory_xact_lock(hashtextextended(
    'lasso|' || v_account || '|' || new.post_date::text, 0));
  if exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso' and c.post_date = new.post_date
       and lower(btrim(coalesce(c.account, ''))) = v_account
       and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
       and coalesce(c.variant_status, 'active') = 'active'
       and coalesce(c.status, 'pending') in ('pending', 'approved', 'publishing')
       and c.id is distinct from new.id
       and (new.slot_index is null and c.slot_index in (0, 1, 2)
            or new.slot_index in (0, 1, 2) and c.slot_index is null)
  ) then
    raise exception 'LASSO Story legacy NULL slot conflicts with numbered Story'
      using errcode = '23505';
  end if;
  return new;
end;
$$;
drop trigger if exists lasso_story_legacy_slot_guard on public.content_calendar;
create trigger lasso_story_legacy_slot_guard
  before insert or update of gym_id, account, post_date, format,
    slot_index, variant_status, status
  on public.content_calendar
  for each row execute function public.lasso_story_legacy_slot_guard();

create or replace function public.stage_lasso_paired_story(
  p_feed_id uuid, p_account text, p_day date, p_slot integer,
  p_feed_status text, p_feed_caption text, p_feed_image_url text,
  p_feed_scheduled_at timestamptz, p_feed_logical_post_id uuid,
  p_story_id uuid, p_story_image_url text, p_story_sha256 text,
  p_artifact_tenant text, p_source_hash text, p_policy_version text,
  p_story_scheduled_at timestamptz
) returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
  v_feed public.content_calendar%rowtype;
  v_existing public.content_calendar%rowtype;
  v_account text;
begin
  v_account := lower(btrim(coalesce(p_account, '')));
  if p_feed_id is null or p_story_id is null or p_feed_id = p_story_id
     or v_account not in ('instagram', 'facebook')
     or p_day < date '2026-10-02'
     or p_slot not in (0, 1, 2)
     or p_feed_status not in ('pending', 'approved', 'published')
     or nullif(btrim(coalesce(p_feed_caption, '')), '') is null
     or p_feed_image_url !~ '^https://'
     or p_story_image_url !~ '^https://'
     or p_feed_image_url = p_story_image_url
     or p_story_sha256 is null or p_story_sha256 !~ '^[0-9a-f]{64}$'
     or p_artifact_tenant not in ('lasso', 'lasso_ig', 'lasso_fb')
     or (p_artifact_tenant = 'lasso_ig' and v_account <> 'instagram')
     or (p_artifact_tenant = 'lasso_fb' and v_account <> 'facebook')
     or p_source_hash is null or p_source_hash !~ '^[0-9a-f]{64}$'
     or nullif(btrim(coalesce(p_policy_version, '')), '') is null
     or p_story_scheduled_at is null
     or (p_story_scheduled_at at time zone 'America/New_York')::date <> p_day
     or (p_feed_scheduled_at is not null and
         p_story_scheduled_at <> p_feed_scheduled_at + interval '15 minutes') then
    return jsonb_build_object('result', 'conflict', 'reason', 'invalid_input');
  end if;

  -- LASSO account/day only. The trigger above takes this same key for legacy
  -- NULL-slot writers; the unique indexes settle exact-slot races.
  perform pg_advisory_xact_lock(hashtextextended(
    'lasso|' || v_account || '|' || p_day::text, 0));
  select * into v_feed from public.content_calendar
   where id = p_feed_id and gym_id = 'lasso'
     and lower(btrim(coalesce(account, ''))) = v_account
     and post_date = p_day
     and lower(btrim(coalesce(format, 'feed'))) = 'feed'
     and slot_index = p_slot and variant_status = 'active'
   for update;
  if not found then
    return jsonb_build_object('result', 'conflict', 'reason', 'source_feed_missing');
  end if;
  if v_feed.status is distinct from p_feed_status
     or v_feed.caption is distinct from p_feed_caption
     or v_feed.image_url is distinct from p_feed_image_url
     or v_feed.scheduled_at is distinct from p_feed_scheduled_at
     or v_feed.logical_post_id is distinct from p_feed_logical_post_id
     or (v_feed.media_not_ready_reason is not null and not
         (v_feed.media_not_ready_reason =
          'prepared_backlog_waiting_for_story_and_capacity'
          and v_feed.status = 'pending'
          and v_feed.post_date between date '2026-10-02' and date '2026-10-05'))
     or (v_feed.status = 'published' and
         (v_feed.published_at is null or v_feed.late_post_id is null))
     or (v_feed.status in ('pending', 'approved') and
         (v_feed.published_at is not null or v_feed.late_post_id is not null)) then
    return jsonb_build_object('result', 'conflict', 'reason', 'source_feed_changed_or_unready');
  end if;

  -- The reviewed Story record must name this exact account's feed row, copy,
  -- media, day and ordinal. A same-date Instagram Story cannot impersonate
  -- Facebook's independently written caption or its feed identity.
  if not exists (
    select 1 from public.echo_infographic_artifacts a
     where a.tenant = p_artifact_tenant and a.image_url = p_story_image_url
       and a.image_sha256 = p_story_sha256
       and a.evidence->>'grade_status' = 'PASS'
       and a.evidence->>'image_sha256' = p_story_sha256
       and a.evidence->>'policy_version' = p_policy_version
       and a.evidence->>'aspect' = '9:16'
       and a.evidence->>'pixels' = '1080x1920'
       and a.evidence->'verified_dimensions'->>'width' = '1080'
       and a.evidence->'verified_dimensions'->>'height' = '1920'
       and a.evidence->'verified_dimensions'->>'image_sha256' = p_story_sha256
       and a.source_identity->>'source_hash' = p_source_hash
       and (
         (p_artifact_tenant = 'lasso'
          and a.source_identity->>'source_feed_id' = p_feed_id::text
          and a.source_identity->>'source_id' =
              'content_calendar:' || p_feed_id::text || ':paired_story'
          and a.source_identity->>'source_account' = v_account
          and a.source_identity->>'source_day' = p_day::text
          and a.source_identity->>'source_slot' = p_slot::text
          and a.source_identity->>'source_feed_caption' = p_feed_caption
          and a.source_identity->>'source_feed_image_url' = p_feed_image_url
          and a.source_identity->>'source_logical_post_id' =
              coalesce(p_feed_logical_post_id::text, ''))
         or
         (p_artifact_tenant in ('lasso_ig', 'lasso_fb')
          and a.source_identity->>'source_id' =
              'content_calendar:' || p_feed_id::text || ':caption'
          and p_source_hash =
              encode(sha256(convert_to(p_feed_caption, 'UTF8')), 'hex'))
       )
  ) then
    return jsonb_build_object('result', 'conflict', 'reason', 'reviewed_9x16_source_mismatch');
  end if;

  select * into v_existing from public.content_calendar where id = p_story_id;
  if found then
    if v_existing.gym_id = 'lasso'
       and lower(btrim(coalesce(v_existing.account, ''))) = v_account
       and v_existing.post_date = p_day
       and lower(btrim(coalesce(v_existing.format, 'feed'))) = 'story'
       and v_existing.slot_index = p_slot
       and v_existing.variant_status = 'active'
       and v_existing.status in ('pending', 'approved')
       and v_existing.caption = ''
       and v_existing.image_url = p_story_image_url
       and v_existing.source_media_url = p_story_image_url
       and v_existing.logical_post_id is not distinct from v_feed.logical_post_id
       and v_existing.scheduled_at is not distinct from p_story_scheduled_at
       and v_existing.media_not_ready_reason is null then
      insert into public.lasso_managed_paired_stories(story_id, feed_id)
      values (p_story_id, p_feed_id)
      on conflict (story_id) do nothing;
      if not exists (select 1 from public.lasso_managed_paired_stories m
                     where m.story_id = p_story_id and m.feed_id = p_feed_id) then
        return jsonb_build_object('result', 'conflict', 'reason', 'managed_pair_conflict');
      end if;
      return jsonb_build_object('result', 'idempotent', 'id', p_story_id);
    end if;
    return jsonb_build_object('result', 'conflict', 'reason', 'id_reused');
  end if;

  if exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso' and c.post_date = p_day
       and lower(btrim(coalesce(c.account, ''))) = v_account
       and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
       and coalesce(c.variant_status, 'active') = 'active'
       and coalesce(c.status, 'pending') not in ('denied', 'killed', 'failed')
       and (c.slot_index = p_slot or c.slot_index is null)
  ) then
    return jsonb_build_object('result', 'conflict', 'reason', 'occupied_story_slot');
  end if;
  if exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso'
       and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
       and coalesce(c.variant_status, 'active') = 'active'
       and coalesce(c.status, 'pending') not in ('denied', 'killed', 'failed')
       and c.image_url = p_story_image_url
  ) then
    return jsonb_build_object('result', 'conflict', 'reason', 'story_media_already_in_use');
  end if;

  insert into public.content_calendar
    (id, gym_id, account, post_date, pillar, format, caption, image_url,
     source_media_url, status, scheduled_at, slot_index, variant_status,
     logical_post_id)
  values
    (p_story_id, 'lasso', v_account, p_day, v_feed.pillar, 'story', '',
     p_story_image_url, p_story_image_url, 'pending', p_story_scheduled_at,
     p_slot, 'active', v_feed.logical_post_id);
  insert into public.lasso_managed_paired_stories(story_id, feed_id)
  values (p_story_id, p_feed_id);
  return jsonb_build_object('result', 'inserted', 'id', p_story_id,
    'feed_id', p_feed_id, 'account', v_account, 'day', p_day, 'slot', p_slot);
exception when unique_violation then
  return jsonb_build_object('result', 'conflict', 'reason', 'unique_violation');
end;
$$;

revoke all on function public.stage_lasso_paired_story(
  uuid,text,date,integer,text,text,text,timestamptz,uuid,uuid,text,text,text,text,text,timestamptz)
  from public, anon, authenticated;
grant execute on function public.stage_lasso_paired_story(
  uuid,text,date,integer,text,text,text,timestamptz,uuid,uuid,text,text,text,text,text,timestamptz)
  to service_role;
commit;
