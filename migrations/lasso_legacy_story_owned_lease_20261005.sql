-- Owned-lease readability for the two pending incident feed/Story pairs.
-- Exactly two feeds (81276931.../af677ffb... and bbd1ae3f.../6391d2b6...)
-- roll back pre-network with leased_feed_source_mismatch: their prepared
-- Story proof is valid while the feed is pending, but once the owned claim
-- flips the feed to 'publishing' the exception below stopped filtering the
-- occupied slot. This migration CREATE OR REPLACEs the original
-- lasso_unrelated_published_story with ONE minimal predicate change: the two
-- require_published = false exception pairs may ALSO be read for
-- f.status = 'publishing' ONLY while the lease is real (an owned claim token
-- AND reservation day, and no publish receipt yet). A bare, stale or
-- incomplete lease fails closed. The third pair (v_require_published) remains
-- published-with-receipt ONLY and is never widened to pending, approved or
-- leased. Every exact UUID/date/pillar/URL/caption/source/tenant guard, the
-- registry/artifact refusal, the grants and the SECURITY DEFINER search_path
-- are preserved verbatim from
-- lasso_paired_story_published_legacy_coexistence_20261005.sql. Read-proof
-- only: no row is claimed, updated or hidden.
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
  v_require_published boolean;
begin
  -- These UUID pairs were independently read from the October 2/4 incident.
  -- A future published Story needs its own source review and migration.
  -- Null url/caption columns mean the pair carries no extra evidence binding.
  select x.story_pillar, x.feed_pillar, x.day, x.slot,
         x.story_url, x.feed_url, x.feed_caption, x.require_published
    into v_story_pillar, v_feed_pillar, v_day, v_slot,
         v_story_url, v_feed_url, v_feed_caption, v_require_published
    from (values
      ('af677ffb-7834-455e-852c-b865a5155ac4'::uuid,
       '81276931-45c8-470b-b658-69a34e4b17a0'::uuid,
       'website'::text, 'platform'::text, date '2026-10-02', 1,
       null::text, null::text, null::text, false),
      ('6391d2b6-9eda-4a1d-b99f-9d6885d3e876'::uuid,
       'bbd1ae3f-ce97-4587-9fca-6ffe114e1c93'::uuid,
       'platform'::text, 'echo'::text, date '2026-10-04', 1,
       null::text, null::text, null::text, false),
      ('810b7a15-d187-4f73-bad9-3fab15954cc0'::uuid,
       '2c6e3489-d965-5abc-b11b-7105047353cf'::uuid,
       'doctrine'::text, 'doctrine'::text, date '2026-10-04', 0,
       'https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/dcad077060d7579d/2026-10-04_810b7a15.png'::text,
       'https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso/91d16553122dcf21/direct_2c6e3489.png'::text,
       'Spend more time with the people in front of you.

LASSO plans your content, creates the posts, and keeps the calendar moving. See the plan in one place.

Save this for later.'::text, true)
    ) as x(story_id, feed_id, story_pillar, feed_pillar, day, slot,
           story_url, feed_url, feed_caption, require_published)
   where x.story_id = p_story_id and x.feed_id = p_feed_id;
  if not found then return false; end if;
  select * into s from public.content_calendar where id = p_story_id;
  select * into f from public.content_calendar where id = p_feed_id;
  -- Every expected-field comparison is null-safe: erased metadata must
  -- refuse, never fall through an IF guard on a NULL predicate.
  if s.id is null or f.id is null
     or s.gym_id is distinct from 'lasso' or f.gym_id is distinct from 'lasso'
     or s.post_date is distinct from f.post_date
     or s.post_date is distinct from v_day
     or lower(btrim(coalesce(s.account, ''))) <> 'instagram'
     or lower(btrim(coalesce(f.account, ''))) <> 'instagram'
     or lower(btrim(coalesce(s.format, 'feed'))) <> 'story'
     or lower(btrim(coalesce(f.format, 'feed'))) <> 'feed'
     or s.slot_index is distinct from v_slot
     or f.slot_index is distinct from v_slot
     or s.variant_status is distinct from 'active'
     or f.variant_status is distinct from 'active'
     or s.status is distinct from 'published' or s.published_at is null
     or s.late_post_id is null
     or coalesce(f.status, '') not in ('pending','approved','published','publishing')
     or (f.status = 'published' and
         (f.published_at is null or f.late_post_id is null))
     or (coalesce(f.status, '') in ('pending','approved') and
         (f.published_at is not null or f.late_post_id is not null))
     -- The only widening over the original function: the two
     -- require_published = false pairs may be read while the feed carries a
     -- REAL owned lease (claim token AND reservation day, no receipt yet).
     -- A bare, stale or incomplete lease fails closed, and the third pair is
     -- never readable here: it remains published-with-receipt only.
     or (f.status = 'publishing' and
         (v_require_published
          or f.publish_claim_token is null
          or f.publish_reservation_day is null
          or f.published_at is not null
          or f.late_post_id is not null))
     or (v_require_published and
         (f.status is distinct from 'published'
          or f.published_at is null or f.late_post_id is null))
     or s.pillar is distinct from v_story_pillar
     or f.pillar is distinct from v_feed_pillar
     or (v_story_url is not null and s.image_url is distinct from v_story_url)
     or (v_feed_url is not null and f.image_url is distinct from v_feed_url)
     or (v_feed_caption is not null
         and f.caption is distinct from v_feed_caption)
     or s.logical_post_id is not null
     or (to_jsonb(s)->>'source_media_asset_id') is not null
     or s.caption is distinct from ''
     or coalesce(s.image_url, '') !~ '^https://'
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
