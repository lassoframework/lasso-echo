-- Exact incident exception for three published legacy Stories which are not
-- the pending feed's Story. Apply after the paired-Story backfill and repair
-- migrations. No historical row is updated or hidden; the publishable-slot
-- unique index and managed source/claim guards remain active. The third pair
-- (slot 0, 2026-10-04) additionally binds the inspected legacy image URL, the
-- published feed image URL, and the raw feed caption (sha256
-- 68df52b993d8ee3af07ac939a7ad4775965fd412540c07d430f04e6c77aae7da), so any
-- changed evidence refuses.
begin;

create or replace function public.lasso_unrelated_published_story(
  p_story_id uuid, p_feed_id uuid
) returns boolean
language plpgsql security definer set search_path = public stable
as $$
declare
  s public.content_calendar%rowtype;
  f public.content_calendar%rowtype;
  v_story_pillar text;
  v_feed_pillar text;
  v_day date;
  v_slot integer;
  v_story_url text;
  v_feed_url text;
  v_feed_caption text;
begin
  -- These UUID pairs were independently read from the October 2/4 incident.
  -- A future published Story needs its own source review and migration.
  -- Null url/caption columns mean the pair carries no extra evidence binding.
  select x.story_pillar, x.feed_pillar, x.day, x.slot,
         x.story_url, x.feed_url, x.feed_caption
    into v_story_pillar, v_feed_pillar, v_day, v_slot,
         v_story_url, v_feed_url, v_feed_caption
    from (values
      ('af677ffb-7834-455e-852c-b865a5155ac4'::uuid,
       '81276931-45c8-470b-b658-69a34e4b17a0'::uuid,
       'website'::text, 'platform'::text, date '2026-10-02', 1,
       null::text, null::text, null::text),
      ('6391d2b6-9eda-4a1d-b99f-9d6885d3e876'::uuid,
       'bbd1ae3f-ce97-4587-9fca-6ffe114e1c93'::uuid,
       'platform'::text, 'echo'::text, date '2026-10-04', 1,
       null::text, null::text, null::text),
      ('810b7a15-d187-4f73-bad9-3fab15954cc0'::uuid,
       '2c6e3489-d965-5abc-b11b-7105047353cf'::uuid,
       'doctrine'::text, 'doctrine'::text, date '2026-10-04', 0,
       'https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/dcad077060d7579d/2026-10-04_810b7a15.png'::text,
       'https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso/91d16553122dcf21/direct_2c6e3489.png'::text,
       'Spend more time with the people in front of you.

LASSO plans your content, creates the posts, and keeps the calendar moving. See the plan in one place.

Save this for later.'::text)
    ) as x(story_id, feed_id, story_pillar, feed_pillar, day, slot,
           story_url, feed_url, feed_caption)
   where x.story_id = p_story_id and x.feed_id = p_feed_id;
  if not found then return false; end if;
  select * into s from public.content_calendar where id = p_story_id;
  select * into f from public.content_calendar where id = p_feed_id;
  if s.id is null or f.id is null
     or s.gym_id <> 'lasso' or f.gym_id <> 'lasso'
     or s.post_date <> f.post_date
     or s.post_date <> v_day
     or lower(btrim(coalesce(s.account, ''))) <> 'instagram'
     or lower(btrim(coalesce(f.account, ''))) <> 'instagram'
     or lower(btrim(coalesce(s.format, 'feed'))) <> 'story'
     or lower(btrim(coalesce(f.format, 'feed'))) <> 'feed'
     or s.slot_index <> v_slot or f.slot_index <> v_slot
     or s.variant_status <> 'active' or f.variant_status <> 'active'
     or s.status <> 'published' or s.published_at is null
     or s.late_post_id is null
     or f.status not in ('pending','approved','published')
     or (f.status = 'published' and
         (f.published_at is null or f.late_post_id is null))
     or (f.status in ('pending','approved') and
         (f.published_at is not null or f.late_post_id is not null))
     or s.pillar is distinct from v_story_pillar
     or f.pillar is distinct from v_feed_pillar
     or (v_story_url is not null and s.image_url is distinct from v_story_url)
     or (v_feed_url is not null and f.image_url is distinct from v_feed_url)
     or (v_feed_caption is not null
         and f.caption is distinct from v_feed_caption)
     or s.logical_post_id is not null
     or (to_jsonb(s)->>'source_media_asset_id') is not null
     or s.caption is distinct from ''
     or s.image_url !~ '^https://'
     or s.source_media_url is distinct from s.image_url then
    return false;
  end if;
  -- A historical Story with any registered or artifact source link to this
  -- feed may already satisfy the pair. Refuse a second Story in that case.
  if exists (select 1 from public.lasso_managed_paired_stories m
             where m.story_id = s.id)
     or exists (
       select 1 from public.echo_infographic_artifacts a
        where a.image_url = s.image_url
          and (a.source_identity->>'source_feed_id' = f.id::text
               or a.source_identity->>'source_id' in (
                  'content_calendar:' || f.id::text || ':paired_story',
                  'content_calendar:' || f.id::text || ':caption')
               or a.source_identity->>'source_feed_image_url' = f.image_url)
     ) then
    return false;
  end if;
  return true;
end;
$$;

revoke all on function public.lasso_unrelated_published_story(uuid,uuid)
  from public, anon, authenticated;
grant execute on function public.lasso_unrelated_published_story(uuid,uuid)
  to service_role;

commit;
