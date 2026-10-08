-- DRAFT / UNAPPLIED / DEFAULT OFF. No production activation is authorized.
-- Forward lock entry, LASSO tranche (child 1): the six production LASSO
-- staging/repair/hold-release functions below acquire the ordinary entry
-- sequence FIRST -- the graph shared lock 'G' (pg_advisory_xact_lock_shared on
-- 'fixer_forward_graph_20261006'), then the census-exclusive transaction entry
-- lock 'C' (pg_advisory_xact_lock on 'fixer_forward_photo_census_20261007') --
-- before ANY advisory, row, or table lock they already take (the per-day
-- 'lasso|<account>|<day>' advisory locks, the FOR UPDATE row locks, and the
-- stage_lasso_campaign_row SHARE ROW EXCLUSIVE table lock). This matches the
-- entry order established by DRAFT_fixer_forward_media_claim_20261006.sql and
-- DRAFT_fixer_forward_lock_entry_calendar_20261008.sql: no caller may hold a
-- row lock, table lock, or tenant advisory lock while waiting on G or C, so a
-- graph/census wait can never invert the lock order. Runtime publishers never
-- upgrade the shared graph lock.
--
-- Prerequisite helper (installed first by the calendar tranche, child 0):
--   public.fixer_forward_calendar_entry_lock_20261008() returns void,
--   language plpgsql, security definer, set search_path=pg_catalog,public.
--   It raises (errcode 25000) unless the transaction isolation is read
--   committed, then takes G (shared) then C (exclusive), in that order.
--   EXECUTE is revoked from public/anon/authenticated/service_role: the
--   replaced LASSO functions are SECURITY DEFINER and reach it as their owner.
--   This migration does NOT redefine the helper; it aborts (23514) unless the
--   helper matches the accepted P2a signature, owner, language, security,
--   search_path and exact isolation/G-then-C body in public.
--
-- Function text: each CREATE OR REPLACE below is the exact frozen production
-- definition from evidence/portal-function-definitions-20261008.sql with ONLY
-- the entry-lock call inserted at the top of the body. Signatures, defaults,
-- SECURITY DEFINER, search_path, return contracts, tenant/approval/
-- idempotency checks, artifact-provenance gates and LASSO autonomous
-- publishing behavior are unchanged. No provider (external API) call exists
-- in these bodies; nothing is added outside DB lock scope.
--
-- Drift precondition: the DO block below aborts the whole migration unless
-- every target function still exists exactly once in public with
-- exact identity arguments and postgres owner from the read-only
-- evidence/portal-entry-function-identities-p2b-20261008.json,
-- applying current_user postgres, and md5(prosrc) equal to the frozen inventory hash
-- (evidence/portal-legacy-function-inventory-20261008.json, query:
-- md5(p.prosrc)). Any production drift -- body edit, drop, or a duplicate
-- overload -- raises 23514 and rolls the transaction back before any replace.
--
-- Rollback before use: restore the frozen definitions from the evidence file.
begin;

-- PREREQUISITE GUARD: the shared entry-lock helper from the calendar tranche
-- must match its accepted P2a definition and postgres ownership in public; this
-- tranche never creates or replaces it.
do $$
declare
  v_count integer;
  v_helper_valid boolean;
begin
  if current_user is distinct from 'postgres' then
    raise exception 'forward lock entry DRAFT: current_user must be postgres' using errcode = '23514';
  end if;
  -- Pin the accepted P2a helper body: read-committed isolation guard,
  -- G shared then C exclusive, no runtime graph upgrade. Exact prosrc hash
  -- also rejects no-op helpers or altered advisory namespaces/order.
  select count(*), bool_and(
           p.pronargs = 0
           and pg_get_function_identity_arguments(p.oid) = ''
           and p.prorettype = 'void'::regtype
           and pg_get_userbyid(p.proowner) = 'postgres'
           and p.prosecdef
           and l.lanname = 'plpgsql'
           and p.proconfig = array['search_path=pg_catalog, public']::text[]
           and md5(p.prosrc) = 'f7804f90164613bff2e70d7f7bc5b4e2')
    into v_count, v_helper_valid
    from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    join pg_language l on l.oid = p.prolang
   where n.nspname = 'public'
     and p.proname = 'fixer_forward_calendar_entry_lock_20261008';
  if v_count is distinct from 1 or v_helper_valid is distinct from true then
    raise exception 'forward lock entry DRAFT: prerequisite helper public.fixer_forward_calendar_entry_lock_20261008() is missing or has the wrong shape (definitions: %); apply the accepted helper draft first',
      v_count using errcode = '23514';
  end if;
end $$;

-- PRECONDITION GUARD: frozen production definitions (abort on any drift).
do $$
declare
  v_name text;
  v_expected text;
  v_expected_identity text;
  v_identity text;
  v_owner text;
  v_count integer;
  v_actual text;
begin
  for v_name, v_expected, v_expected_identity in
    select f.proname, f.body_md5, f.identity_args from (values
      ('release_lasso_backlog_feed_hold', '064c5ab91745ed8fca62eccc5d98937c', 'p_feed_id uuid, p_story_id uuid, p_expected_feed jsonb, p_expected_story jsonb'),
      ('release_lasso_paired_story_hold', '3e8cc9923f8ba4c715bf2d8468c97e86', 'p_story_id uuid'),
      ('repair_lasso_paired_story', '016c1f1d7f72f73b510b4e48c43eea08', 'p_story_id uuid, p_feed_id uuid, p_expected_story jsonb, p_expected_feed jsonb, p_story_image_url text, p_story_sha256 text, p_artifact_tenant text, p_source_hash text, p_policy_version text, p_story_scheduled_at timestamp with time zone'),
      ('stage_lasso_campaign_row', '2c0dadddcffee17506675c0bdb11b59f', 'p_row jsonb, p_artifact_tenant text, p_source_hash text, p_policy_version text, p_image_sha256 text'),
      ('stage_lasso_paired_story', 'c151abbe81bdca29e9dee9ddf22a6089', 'p_feed_id uuid, p_account text, p_day date, p_slot integer, p_feed_status text, p_feed_caption text, p_feed_image_url text, p_feed_scheduled_at timestamp with time zone, p_feed_logical_post_id uuid, p_story_id uuid, p_story_image_url text, p_story_sha256 text, p_artifact_tenant text, p_source_hash text, p_policy_version text, p_story_scheduled_at timestamp with time zone'),
      ('stage_lasso_third_story', '37872cec68ee59dda43344a0442855d2', 'p_feed_id uuid, p_feed_caption text, p_feed_image_url text, p_story_id uuid, p_story_image_url text, p_story_source_url text, p_story_sha256 text, p_source_hash text, p_policy_version text, p_scheduled_at timestamp with time zone, p_caption_hash text')
    ) as f(proname, body_md5, identity_args)
  loop
    select count(*), max(md5(p.prosrc)),
           max(pg_get_function_identity_arguments(p.oid)), max(pg_get_userbyid(p.proowner))
      into v_count, v_actual, v_identity, v_owner
      from pg_proc p join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public' and p.proname = v_name;
    if v_count is distinct from 1 or v_actual is distinct from v_expected
        or v_identity is distinct from v_expected_identity
        or v_owner is distinct from 'postgres' then
      raise exception 'forward lock entry LASSO DRAFT: production definition of % drifted from the frozen 2026-10-08 inventory (count %, md5 %, identity %, owner %); aborting before any replace',
        v_name, v_count, v_actual, v_identity, v_owner using errcode = '23514';
    end if;
  end loop;
end $$;

CREATE OR REPLACE FUNCTION public.release_lasso_backlog_feed_hold(p_feed_id uuid, p_story_id uuid, p_expected_feed jsonb, p_expected_story jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  f public.content_calendar%rowtype;
  s public.content_calendar%rowtype;
  v_account text;
  v_day date;
  v_count integer;
begin
  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census
  -- exclusive 'C', before any advisory, row, or table lock below.
  perform public.fixer_forward_calendar_entry_lock_20261008();
  if p_feed_id is null or p_story_id is null or p_feed_id = p_story_id
     or jsonb_typeof(p_expected_feed) <> 'object'
     or jsonb_typeof(p_expected_story) <> 'object'
     or not (p_expected_feed ?& array['id','gym_id','account','post_date',
       'slot_index','format','status','variant_status','caption','image_url',
       'pillar','scheduled_at','logical_post_id','media_not_ready_reason',
       'published_at','late_post_id','publish_claim_token',
       'publish_reservation_day'])
     or not (p_expected_story ?& array['id','gym_id','account','post_date',
       'slot_index','format','status','variant_status','caption','image_url',
       'source_media_url','pillar','scheduled_at','logical_post_id',
       'media_not_ready_reason','published_at','late_post_id',
       'publish_claim_token','publish_reservation_day'])
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
        nullif(p_expected_feed->>'publish_claim_token','')::uuid
     or f.publish_reservation_day is distinct from
        nullif(p_expected_feed->>'publish_reservation_day','')::date then
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
        nullif(p_expected_story->>'publish_claim_token','')::uuid
     or s.publish_reservation_day is distinct from
        nullif(p_expected_story->>'publish_reservation_day','')::date then
    return jsonb_build_object('result','conflict','reason','story_changed');
  end if;
  select count(*) into v_count from public.content_calendar c
   where c.gym_id = 'lasso' and c.post_date = v_day
     and lower(btrim(coalesce(c.account,''))) = v_account
     and lower(btrim(coalesce(c.format,'feed'))) = 'story'
     and c.slot_index = f.slot_index
     and coalesce(c.variant_status,'active') = 'active'
     and c.status = 'pending';
  if v_count <> 1 or exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso' and c.post_date = v_day
       and lower(btrim(coalesce(c.account,''))) = v_account
       and lower(btrim(coalesce(c.format,'feed'))) = 'story'
       and coalesce(c.variant_status,'active') = 'active'
       and (c.status is null or c.status not in ('denied','killed','failed'))
       and c.id <> s.id
       and (c.slot_index is null or c.slot_index = f.slot_index)
       and (c.status is distinct from 'published'
            or not public.lasso_unrelated_published_story(c.id, f.id))) then
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
$function$
;

CREATE OR REPLACE FUNCTION public.release_lasso_paired_story_hold(p_story_id uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  s public.content_calendar%rowtype;
  f public.content_calendar%rowtype;
begin
  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census
  -- exclusive 'C', before any advisory, row, or table lock below.
  perform public.fixer_forward_calendar_entry_lock_20261008();
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
      and not public.lasso_unrelated_published_story(c.id, f.id)
  ) then
    return jsonb_build_object('result','conflict','reason','historical_story_already_published');
  end if;
  update public.content_calendar set media_not_ready_reason = null
   where id = s.id;
  return jsonb_build_object('result','released','id',s.id,'feed_id',f.id);
end;
$function$
;

CREATE OR REPLACE FUNCTION public.repair_lasso_paired_story(p_story_id uuid, p_feed_id uuid, p_expected_story jsonb, p_expected_feed jsonb, p_story_image_url text, p_story_sha256 text, p_artifact_tenant text, p_source_hash text, p_policy_version text, p_story_scheduled_at timestamp with time zone)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  s public.content_calendar%rowtype;
  f public.content_calendar%rowtype;
  v_account text;
  v_reason text;
begin
  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census
  -- exclusive 'C', before any advisory, row, or table lock below.
  perform public.fixer_forward_calendar_entry_lock_20261008();
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
     or s.post_date < date '2026-10-02'
     or s.slot_index not in (0, 1, 2)
     or s.status <> 'pending' or s.variant_status <> 'active'
     or s.published_at is not null or s.late_post_id is not null
     or s.publish_claim_token is not null
     or s.media_not_ready_reason not in ('paired_feed_not_ready',
                                         'caption_changed_needs_new_visual',
                                         'cross_date_media_repeat_needs_new_visual')
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
     or (f.media_not_ready_reason is not null and not
         (f.media_not_ready_reason =
          'prepared_backlog_waiting_for_story_and_capacity'
          and f.status = 'pending'
          and f.post_date between date '2026-10-02' and date '2026-10-05'))
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
          and a.evidence->>'pixels' = '1080x1920'
          and a.evidence->'verified_dimensions'->>'width' = '1080'
          and a.evidence->'verified_dimensions'->>'height' = '1920'
          and a.evidence->'verified_dimensions'->>'image_sha256' = p_story_sha256
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
      and not public.lasso_unrelated_published_story(c.id, f.id)
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
$function$
;

CREATE OR REPLACE FUNCTION public.stage_lasso_campaign_row(p_row jsonb, p_artifact_tenant text, p_source_hash text, p_policy_version text, p_image_sha256 text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_id uuid;
  v_day date;
  v_account text;
  v_slot integer;
  v_image_url text;
  v_caption text;
  v_scheduled_at timestamptz;
  v_existing public.content_calendar%rowtype;
begin
  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census
  -- exclusive 'C', before any advisory, row, or table lock below.
  perform public.fixer_forward_calendar_entry_lock_20261008();
  if p_row is null or jsonb_typeof(p_row) <> 'object' then
    return jsonb_build_object('result', 'conflict', 'reason', 'invalid_row');
  end if;

  -- Reject invented/unknown provenance and every calendar field this narrow
  -- staging path is not allowed to write.  In particular, the production
  -- calendar schema has no category or draft_type column.
  if exists (
    select 1 from jsonb_object_keys(p_row) as supplied(key)
    where supplied.key not in (
      'id', 'gym_id', 'account', 'post_date', 'pillar', 'format', 'caption',
      'image_url', 'status', 'scheduled_at', 'slot_index', 'variant_status'
    )
  ) then
    return jsonb_build_object('result', 'conflict', 'reason', 'unsupported_row_key');
  end if;

  begin
    v_id := nullif(p_row->>'id', '')::uuid;
    v_day := nullif(p_row->>'post_date', '')::date;
    v_slot := nullif(p_row->>'slot_index', '')::integer;
    v_scheduled_at := nullif(p_row->>'scheduled_at', '')::timestamptz;
  exception when others then
    return jsonb_build_object('result', 'conflict', 'reason', 'invalid_typed_value');
  end;
  v_account := lower(btrim(coalesce(p_row->>'account', '')));
  v_image_url := btrim(coalesce(p_row->>'image_url', ''));
  v_caption := coalesce(p_row->>'caption', '');

  if v_id is null
      or v_day is null
      or p_row->>'gym_id' is distinct from 'lasso'
      or v_day not between date '2026-09-23' and date '2026-11-08'
      or v_account not in ('instagram', 'facebook')
      or lower(btrim(coalesce(p_row->>'format', ''))) <> 'feed'
      or lower(btrim(coalesce(p_row->>'status', ''))) <> 'pending'
      or lower(btrim(coalesce(p_row->>'variant_status', ''))) <> 'active'
      or v_slot is null
      or v_slot not in (0, 1, 2)
      or nullif(btrim(coalesce(p_row->>'pillar', '')), '') is null
      or nullif(v_caption, '') is null
      or nullif(v_image_url, '') is null
      or nullif(btrim(coalesce(p_artifact_tenant, '')), '') is null
      or p_artifact_tenant not in ('lasso', 'lasso_ig')
      or p_source_hash is null
      or p_source_hash !~ '^[0-9a-f]{64}$'
      or p_image_sha256 is null
      or p_image_sha256 !~ '^[0-9a-f]{64}$'
      or nullif(btrim(coalesce(p_policy_version, '')), '') is null then
    return jsonb_build_object('result', 'conflict', 'reason', 'out_of_scope');
  end if;
  if (v_slot = 2) <> (lower(btrim(p_row->>'pillar')) = 'summit') then
    return jsonb_build_object('result', 'conflict', 'reason', 'pillar_slot_mismatch');
  end if;

  -- Serialize against every ordinary content_calendar writer, not only callers
  -- that voluntarily take the campaign advisory lock.
  lock table public.content_calendar in share row exclusive mode;

  if not exists (
    select 1
      from public.echo_infographic_artifacts a
     where a.tenant = p_artifact_tenant
       and a.image_url = v_image_url
       and a.image_sha256 = p_image_sha256
       and a.evidence->>'grade_status' = 'PASS'
       and a.evidence->>'image_sha256' = p_image_sha256
       and a.evidence->>'policy_version' = p_policy_version
       and a.source_identity->>'source_hash' = p_source_hash
  ) then
    return jsonb_build_object('result', 'conflict', 'reason', 'reviewed_artifact_mismatch');
  end if;

  select * into v_existing
    from public.content_calendar
   where id = v_id;
  if found then
    if v_existing.gym_id = 'lasso'
       and lower(btrim(coalesce(v_existing.account, ''))) = v_account
       and v_existing.post_date = v_day
       and lower(btrim(coalesce(v_existing.format, 'feed'))) = 'feed'
       and v_existing.status = 'pending'
       and v_existing.variant_status = 'active'
       and v_existing.slot_index = v_slot
       and v_existing.pillar = p_row->>'pillar'
       and v_existing.caption = v_caption
       and v_existing.image_url = v_image_url
       and v_existing.scheduled_at is not distinct from v_scheduled_at then
      return jsonb_build_object('result', 'idempotent', 'id', v_id);
    end if;
    return jsonb_build_object('result', 'conflict', 'reason', 'id_reused_with_different_row', 'id', v_id);
  end if;

  -- An active occupying row owns its account/day/slot. Protected rows are read
  -- as conflicts and are never modified by this function.
  if exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso'
       and c.post_date = v_day
       and lower(btrim(coalesce(c.account, ''))) = v_account
       and lower(btrim(coalesce(c.format, 'feed'))) = 'feed'
       and c.variant_status = 'active'
       and c.status in ('pending', 'approved', 'publishing', 'published', 'draft')
       -- A legacy active row with no ordinal makes the logical shape ambiguous;
       -- fail closed until it is separately normalized under review.
       and (c.slot_index = v_slot or c.slot_index is null)
  ) then
    return jsonb_build_object('result', 'conflict', 'reason', 'occupied_logical_slot');
  end if;

  -- Instagram and Facebook are the two rows of one logical creative and may
  -- share media/copy only when date and slot match. Reuse by the same account,
  -- another logical slot, or another campaign day is refused.
  if exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso'
       and lower(btrim(coalesce(c.format, 'feed'))) = 'feed'
       and c.variant_status = 'active'
       and c.status in ('pending', 'approved', 'publishing', 'published', 'draft')
       and c.post_date between date '2026-09-23' and date '2026-11-08'
       and (c.image_url = v_image_url or c.caption = v_caption)
       and not (
         c.post_date = v_day
         and c.slot_index = v_slot
         and lower(btrim(coalesce(c.account, ''))) in ('instagram', 'facebook')
         and lower(btrim(coalesce(c.account, ''))) <> v_account
       )
  ) then
    return jsonb_build_object('result', 'conflict', 'reason', 'cross_day_creative_reuse');
  end if;

  insert into public.content_calendar (
    id, gym_id, account, post_date, pillar, format, caption, image_url,
    status, scheduled_at, slot_index, variant_status
  ) values (
    v_id, 'lasso', v_account, v_day, p_row->>'pillar', 'feed', v_caption,
    v_image_url, 'pending', v_scheduled_at,
    v_slot, 'active'
  );

  return jsonb_build_object('result', 'inserted', 'id', v_id);
exception
  when unique_violation then
    -- Defensive only: the table lock makes an ordinary concurrent insert wait.
    return jsonb_build_object('result', 'conflict', 'reason', 'unique_violation');
end;
$function$
;

CREATE OR REPLACE FUNCTION public.stage_lasso_paired_story(p_feed_id uuid, p_account text, p_day date, p_slot integer, p_feed_status text, p_feed_caption text, p_feed_image_url text, p_feed_scheduled_at timestamp with time zone, p_feed_logical_post_id uuid, p_story_id uuid, p_story_image_url text, p_story_sha256 text, p_artifact_tenant text, p_source_hash text, p_policy_version text, p_story_scheduled_at timestamp with time zone)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_feed public.content_calendar%rowtype;
  v_existing public.content_calendar%rowtype;
  v_account text;
begin
  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census
  -- exclusive 'C', before any advisory, row, or table lock below.
  perform public.fixer_forward_calendar_entry_lock_20261008();
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
       and (c.status <> 'published' or c.slot_index is null
            or not public.lasso_unrelated_published_story(c.id, p_feed_id))
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
$function$
;

CREATE OR REPLACE FUNCTION public.stage_lasso_third_story(p_feed_id uuid, p_feed_caption text, p_feed_image_url text, p_story_id uuid, p_story_image_url text, p_story_source_url text, p_story_sha256 text, p_source_hash text, p_policy_version text, p_scheduled_at timestamp with time zone, p_caption_hash text DEFAULT NULL::text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_feed public.content_calendar%rowtype;
  v_existing public.content_calendar%rowtype;
  v_day date;
begin
  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census
  -- exclusive 'C', before any advisory, row, or table lock below.
  perform public.fixer_forward_calendar_entry_lock_20261008();
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

  -- The source row lock, per-day advisory lock, and unique active slot index
  -- coordinate this insert without blocking calendar writers for other gyms.
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
  if v_day not between date '2026-09-23' and date '2026-11-08'
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
     where a.tenant in ('lasso', 'lasso_ig') and a.image_url = p_feed_image_url
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
$function$
;

commit;
