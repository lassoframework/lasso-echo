-- Owned conversion of the five evidenced legacy standalone LASSO Stories
-- (Nov 1/2/4/5/6, explicit NULL slot/logical/schedule) into managed paired
-- slot-2 Summit Stories bound to the five evidenced existing feed UUIDs.
-- Service-role-only atomic RPC plus a reviewed immutable manifest and an
-- append-only immutable audit table. Every UUID and full before-state is
-- preserved; nothing is deleted, re-dated, approved, published, unheld or
-- claimed. The conversion never infers pairing from date/media/caption: it
-- requires the exact reviewed manifest preimages, exact full-key-set
-- compare-and-swap of the complete current Story and feed snapshots, fresh
-- autonomy, a ready feed (NON-NULL pending/approved, no claim/reservation/
-- receipt/hold, fresh caption-bound PASS artwork), a current Brain/policy-PASS
-- measured 9:16 Story artifact bound to the feed's CURRENT caption only, and
-- the canonical noon feed / 12:15 Story America/New_York schedule. The
-- production feeds currently carry NULL schedules and held media; root must
-- review/stamp the canonical schedule and fresh media before conversion —
-- these gates are NOT weakened. Root owns production application and live
-- verification.
begin;

-- Append-only guard shared by the manifest and the audit table.
create or replace function public.lasso_story_conversion_immutable_20261008()
returns trigger language plpgsql
as $$
begin
  raise exception 'lasso standalone story conversion record immutable'
    using errcode = '23514';
end;
$$;

-- Reviewed manifest: exactly the five evidenced legacy Stories and their five
-- evidenced existing feeds with complete DB preimages (read-only production
-- queries 2026-10-08T22:38Z and 2026-10-08T23:14Z). The story preimages carry
-- the full production key set; only the diagnostic managed_links field was
-- stripped. Immutable.
create table public.lasso_standalone_story_conversion_manifest_20261008 (
  story_id uuid primary key,
  feed_id uuid not null unique,
  account text not null,
  post_date date not null,
  old_image_url text not null,
  expected_story jsonb not null,
  expected_feed jsonb not null,
  reviewed_at timestamptz not null default now()
);
insert into public.lasso_standalone_story_conversion_manifest_20261008
  (story_id, feed_id, account, post_date, old_image_url, expected_story, expected_feed) values
 ('1d591c59-32c1-48a8-963f-ad1bce32ad00','47353415-893c-5372-954a-66121900a064','instagram',date '2026-11-01',
  'https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/0eb19dca872c513f/11.png',
  '{"id":"1d591c59-32c1-48a8-963f-ad1bce32ad00","gym_id":"lasso","account":"instagram","post_date":"2026-11-01","pillar":"summit","format":"story","caption":"","image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/0eb19dca872c513f/11.png","status":"pending","created_at":"2026-08-07 22:03:28.274168+00","published_at":null,"late_post_id":null,"scheduled_at":null,"thumbnail_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/0eb19dca872c513f/11.png","gbp_topic_type":null,"gbp_cta_type":null,"gbp_cta_url":null,"gbp_event":null,"gbp_offer":null,"gbp_location_id":null,"reject_reason":"","source_media_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/0eb19dca872c513f/11.png","mentions":[],"hook_family":"bold_claim","ask_type":"none","time_slot":"early_morning","caption_len_band":"short","has_member_face":null,"experiment_label":null,"slot_index":null,"source_media_asset_id":null,"media_not_ready_reason":null,"event_id":null,"variant_of":null,"variant_status":"active","publish_reservation_day":null,"publish_claim_token":null,"logical_post_id":null,"approval_kind":null,"approved_by":null,"approved_at":null,"approval_digest":null}'::jsonb,
  '{"id":"47353415-893c-5372-954a-66121900a064","format":"feed","gym_id":"lasso","pillar":"summit","status":"pending","account":"instagram","caption":"Your gym does not need another vague goal. It needs a 2027 growth playbook built for the work ahead. Get tickets at lassoframework.com/summit","ask_type":"none","event_id":null,"mentions":[],"gbp_event":null,"gbp_offer":null,"image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso/cf568307cc2551ea/summitpeople-2026-10-29.png","post_date":"2026-11-01","time_slot":"evening","created_at":"2026-09-23T20:03:22.067242+00:00","slot_index":2,"variant_of":null,"approved_at":null,"approved_by":null,"gbp_cta_url":null,"hook_family":"bold_claim","gbp_cta_type":null,"late_post_id":null,"published_at":null,"scheduled_at":null,"approval_kind":null,"reject_reason":null,"thumbnail_url":null,"gbp_topic_type":null,"variant_status":"active","approval_digest":null,"gbp_location_id":null,"has_member_face":null,"logical_post_id":null,"caption_len_band":"short","experiment_label":null,"source_media_url":null,"publish_claim_token":null,"source_media_asset_id":null,"media_not_ready_reason":"cross_date_media_repeat_needs_new_visual","publish_reservation_day":null}'::jsonb),
 ('47ea9c06-6d0e-45f3-a95f-20252b82cf03','7e1ad844-1b18-5352-8287-8825fe54db99','instagram',date '2026-11-02',
  'https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/0eb19dca872c513f/11.png',
  '{"id":"47ea9c06-6d0e-45f3-a95f-20252b82cf03","gym_id":"lasso","account":"instagram","post_date":"2026-11-02","pillar":"summit","format":"story","caption":"","image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/0eb19dca872c513f/11.png","status":"pending","created_at":"2026-08-07 22:03:28.274168+00","published_at":null,"late_post_id":null,"scheduled_at":null,"thumbnail_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/0eb19dca872c513f/11.png","gbp_topic_type":null,"gbp_cta_type":null,"gbp_cta_url":null,"gbp_event":null,"gbp_offer":null,"gbp_location_id":null,"reject_reason":"","source_media_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/0eb19dca872c513f/11.png","mentions":[],"hook_family":"bold_claim","ask_type":"none","time_slot":"early_morning","caption_len_band":"short","has_member_face":null,"experiment_label":null,"slot_index":null,"source_media_asset_id":null,"media_not_ready_reason":"cross_date_media_repeat_needs_new_visual","event_id":null,"variant_of":null,"variant_status":"active","publish_reservation_day":null,"publish_claim_token":null,"logical_post_id":null,"approval_kind":null,"approved_by":null,"approved_at":null,"approval_digest":null}'::jsonb,
  '{"id":"7e1ad844-1b18-5352-8287-8825fe54db99","format":"feed","gym_id":"lasso","pillar":"summit","status":"pending","account":"instagram","caption":"A weekend of real strategy can create months of better decisions.\n\nMeet us at Virgin Hotels Nashville on November 7 and 8. Register at lassoframework.com/summit","ask_type":"none","event_id":null,"mentions":[],"gbp_event":null,"gbp_offer":null,"image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso/9692e087c23e1611/summitpeople-2026-10-30.png","post_date":"2026-11-02","time_slot":null,"created_at":"2026-09-23T20:03:22.871485+00:00","slot_index":2,"variant_of":null,"approved_at":null,"approved_by":null,"gbp_cta_url":null,"hook_family":"bold_claim","gbp_cta_type":null,"late_post_id":null,"published_at":null,"scheduled_at":null,"approval_kind":null,"reject_reason":null,"thumbnail_url":null,"gbp_topic_type":null,"variant_status":"active","approval_digest":null,"gbp_location_id":null,"has_member_face":null,"logical_post_id":null,"caption_len_band":"mid","experiment_label":null,"source_media_url":null,"publish_claim_token":null,"source_media_asset_id":null,"media_not_ready_reason":"cross_date_media_repeat_needs_new_visual","publish_reservation_day":null}'::jsonb),
 ('6df42648-2c7f-4522-bc6d-37a98eb8ad3d','4db9ec0e-e762-5f8e-8a18-ad7ea56cad07','instagram',date '2026-11-04',
  'https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/8863e022991b4e64/16.png',
  '{"id":"6df42648-2c7f-4522-bc6d-37a98eb8ad3d","gym_id":"lasso","account":"instagram","post_date":"2026-11-04","pillar":"summit","format":"story","caption":"","image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/8863e022991b4e64/16.png","status":"pending","created_at":"2026-08-07 22:03:28.274168+00","published_at":null,"late_post_id":null,"scheduled_at":null,"thumbnail_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/8863e022991b4e64/16.png","gbp_topic_type":null,"gbp_cta_type":null,"gbp_cta_url":null,"gbp_event":null,"gbp_offer":null,"gbp_location_id":null,"reject_reason":"","source_media_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/8863e022991b4e64/16.png","mentions":[],"hook_family":"bold_claim","ask_type":"none","time_slot":"early_morning","caption_len_band":"short","has_member_face":null,"experiment_label":null,"slot_index":null,"source_media_asset_id":null,"media_not_ready_reason":null,"event_id":null,"variant_of":null,"variant_status":"active","publish_reservation_day":null,"publish_claim_token":null,"logical_post_id":null,"approval_kind":null,"approved_by":null,"approved_at":null,"approval_digest":null}'::jsonb,
  '{"id":"4db9ec0e-e762-5f8e-8a18-ad7ea56cad07","format":"feed","gym_id":"lasso","pillar":"summit","status":"pending","account":"instagram","caption":"This week in Nashville.\n\nBring the questions that deserve more than a quick answer and build the next plan with us. Get tickets at lassoframework.com/summit","ask_type":"none","event_id":null,"mentions":[],"gbp_event":null,"gbp_offer":null,"image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso/c749f4f6a4de6c88/summitpeople-2026-11-01.png","post_date":"2026-11-04","time_slot":null,"created_at":"2026-09-23T20:03:24.538146+00:00","slot_index":2,"variant_of":null,"approved_at":null,"approved_by":null,"gbp_cta_url":null,"hook_family":"bold_claim","gbp_cta_type":null,"late_post_id":null,"published_at":null,"scheduled_at":null,"approval_kind":null,"reject_reason":null,"thumbnail_url":null,"gbp_topic_type":null,"variant_status":"active","approval_digest":null,"gbp_location_id":null,"has_member_face":null,"logical_post_id":null,"caption_len_band":"mid","experiment_label":null,"source_media_url":null,"publish_claim_token":null,"source_media_asset_id":null,"media_not_ready_reason":"cross_date_media_repeat_needs_new_visual","publish_reservation_day":null}'::jsonb),
 ('53e0fa31-8676-4624-a877-673883859509','96ef0beb-bd99-5358-bb75-820085ecc92e','instagram',date '2026-11-05',
  'https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/ceb33e0112d7eb80/20.png',
  '{"id":"53e0fa31-8676-4624-a877-673883859509","gym_id":"lasso","account":"instagram","post_date":"2026-11-05","pillar":"summit","format":"story","caption":"","image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/ceb33e0112d7eb80/20.png","status":"pending","created_at":"2026-08-07 22:03:28.274168+00","published_at":null,"late_post_id":null,"scheduled_at":null,"thumbnail_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/ceb33e0112d7eb80/20.png","gbp_topic_type":null,"gbp_cta_type":null,"gbp_cta_url":null,"gbp_event":null,"gbp_offer":null,"gbp_location_id":null,"reject_reason":"","source_media_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/ceb33e0112d7eb80/20.png","mentions":[],"hook_family":"bold_claim","ask_type":"none","time_slot":"early_morning","caption_len_band":"short","has_member_face":null,"experiment_label":null,"slot_index":null,"source_media_asset_id":null,"media_not_ready_reason":null,"event_id":null,"variant_of":null,"variant_status":"active","publish_reservation_day":null,"publish_claim_token":null,"logical_post_id":null,"approval_kind":null,"approved_by":null,"approved_at":null,"approval_digest":null}'::jsonb,
  '{"id":"96ef0beb-bd99-5358-bb75-820085ecc92e","format":"feed","gym_id":"lasso","pillar":"summit","status":"pending","account":"instagram","caption":"Seats are selling fast.\n\nGet your tickets now. The calendar is about to turn. Give your gym a plan with enough detail to use on Monday. Register at lassoframework.com/summit","ask_type":"none","event_id":null,"mentions":[],"gbp_event":null,"gbp_offer":null,"image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso/b0a128ec5eb39980/summitpeople-2026-11-02.png","post_date":"2026-11-05","time_slot":null,"created_at":"2026-09-23T20:03:25.394509+00:00","slot_index":2,"variant_of":null,"approved_at":null,"approved_by":null,"gbp_cta_url":null,"hook_family":"bold_claim","gbp_cta_type":null,"late_post_id":null,"published_at":null,"scheduled_at":null,"approval_kind":null,"reject_reason":null,"thumbnail_url":null,"gbp_topic_type":null,"variant_status":"active","approval_digest":null,"gbp_location_id":null,"has_member_face":null,"logical_post_id":null,"caption_len_band":"mid","experiment_label":null,"source_media_url":null,"publish_claim_token":null,"source_media_asset_id":null,"media_not_ready_reason":"cross_date_media_repeat_needs_new_visual","publish_reservation_day":null}'::jsonb),
 ('46a30cd8-cff1-42b4-82b4-ce2c8acc14cc','df8aabea-1ddb-5b44-9b31-f57eb8bc8202','instagram',date '2026-11-06',
  'https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/8faf74d20266cc5b/18.png',
  '{"id":"46a30cd8-cff1-42b4-82b4-ce2c8acc14cc","gym_id":"lasso","account":"instagram","post_date":"2026-11-06","pillar":"summit","format":"story","caption":"","image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/8faf74d20266cc5b/18.png","status":"pending","created_at":"2026-08-07 22:03:28.274168+00","published_at":null,"late_post_id":null,"scheduled_at":null,"thumbnail_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/8faf74d20266cc5b/18.png","gbp_topic_type":null,"gbp_cta_type":null,"gbp_cta_url":null,"gbp_event":null,"gbp_offer":null,"gbp_location_id":null,"reject_reason":"","source_media_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/8faf74d20266cc5b/18.png","mentions":[],"hook_family":"bold_claim","ask_type":"none","time_slot":"early_morning","caption_len_band":"short","has_member_face":null,"experiment_label":null,"slot_index":null,"source_media_asset_id":null,"media_not_ready_reason":null,"event_id":null,"variant_of":null,"variant_status":"active","publish_reservation_day":null,"publish_claim_token":null,"logical_post_id":null,"approval_kind":null,"approved_by":null,"approved_at":null,"approval_digest":null}'::jsonb,
  '{"id":"df8aabea-1ddb-5b44-9b31-f57eb8bc8202","format":"feed","gym_id":"lasso","pillar":"summit","status":"pending","account":"instagram","caption":"The LASSO Growth Summit puts serious operators in one place for two focused days.\n\nNashville is this Saturday and Sunday. Claim your seat at lassoframework.com/summit","ask_type":"booking_link","event_id":null,"mentions":[],"gbp_event":null,"gbp_offer":null,"image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso/986fa0781d47f19e/summitpeople-2026-11-03.png","post_date":"2026-11-06","time_slot":null,"created_at":"2026-09-23T20:03:26.149202+00:00","slot_index":2,"variant_of":null,"approved_at":null,"approved_by":null,"gbp_cta_url":null,"hook_family":"bold_claim","gbp_cta_type":null,"late_post_id":null,"published_at":null,"scheduled_at":null,"approval_kind":null,"reject_reason":null,"thumbnail_url":null,"gbp_topic_type":null,"variant_status":"active","approval_digest":null,"gbp_location_id":null,"has_member_face":null,"logical_post_id":null,"caption_len_band":"mid","experiment_label":null,"source_media_url":null,"publish_claim_token":null,"source_media_asset_id":null,"media_not_ready_reason":"cross_date_media_repeat_needs_new_visual","publish_reservation_day":null}'::jsonb);

-- Append-only immutable conversion receipt: full before-state, full actual
-- after-Story state and exact target linkage. No update/delete/truncate ever; not readable by app roles.
create table public.lasso_standalone_story_conversion_audit_20261008 (
  story_id uuid primary key,
  feed_id uuid not null,
  converted_at timestamptz not null default now(),
  before_story jsonb not null,
  before_feed jsonb not null,
  after_story jsonb not null,
  new_image_url text not null,
  new_image_sha256 text not null,
  artifact_tenant text not null,
  source_hash text not null,
  policy_version text not null,
  brain_snapshot jsonb not null,
  story_scheduled_at timestamptz not null,
  logical_post_id uuid,
  hold_reason text not null
);

create trigger manifest_immutable_row before update or delete
  on public.lasso_standalone_story_conversion_manifest_20261008
  for each row execute function public.lasso_story_conversion_immutable_20261008();
create trigger manifest_immutable_truncate before truncate
  on public.lasso_standalone_story_conversion_manifest_20261008
  for each statement execute function public.lasso_story_conversion_immutable_20261008();
create trigger audit_immutable_row before update or delete
  on public.lasso_standalone_story_conversion_audit_20261008
  for each row execute function public.lasso_story_conversion_immutable_20261008();
create trigger audit_immutable_truncate before truncate
  on public.lasso_standalone_story_conversion_audit_20261008
  for each statement execute function public.lasso_story_conversion_immutable_20261008();

-- Compare complete JSON snapshots without treating empty/naive timestamps as
-- NULL or interpreting them in the caller's session timezone. JSON null is
-- distinct from every string. Explicit offsets/Z normalize to the same instant.
create or replace function public.lasso_conversion_snapshot_20261008(p_snapshot jsonb)
returns jsonb language plpgsql immutable set search_path = public
as $$
declare
  result jsonb := p_snapshot;
  k text;
  v jsonb;
begin
  if jsonb_typeof(p_snapshot) is distinct from 'object' then return null; end if;
  foreach k in array array['created_at','published_at','scheduled_at','approved_at'] loop
    if not p_snapshot ? k then return null; end if;
    v := p_snapshot->k;
    if v = 'null'::jsonb then continue; end if;
    if jsonb_typeof(v) is distinct from 'string'
       or (p_snapshot->>k) !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}(:?[0-9]{2})?)$' then
      return null;
    end if;
    result := jsonb_set(result, array[k],
                        to_jsonb(extract(epoch from (p_snapshot->>k)::timestamptz)));
  end loop;
  return result;
exception when invalid_datetime_format or datetime_field_overflow
               or invalid_time_zone_displacement_value then
  return null;
end;
$$;

create or replace function public.convert_lasso_standalone_story_20261008(
  p_story_id uuid, p_feed_id uuid,
  p_expected_story jsonb, p_expected_feed jsonb,
  p_story_image_url text, p_story_sha256 text,
  p_artifact_tenant text, p_source_hash text, p_policy_version text,
  p_brain_snapshot jsonb, p_story_scheduled_at timestamptz
) returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
  m public.lasso_standalone_story_conversion_manifest_20261008%rowtype;
  a public.lasso_standalone_story_conversion_audit_20261008%rowtype;
  s public.content_calendar%rowtype;
  f public.content_calendar%rowtype;
  replay boolean := false;
  story_input jsonb;
  feed_input jsonb;
begin
  -- Strict input shape. Unknown ids, malformed hashes/urls, non-object
  -- snapshots, missing identity, blank policy or empty Brain all refuse.
  if p_story_id is null or p_feed_id is null or p_story_id = p_feed_id
     or jsonb_typeof(p_expected_story) is distinct from 'object'
     or jsonb_typeof(p_expected_feed) is distinct from 'object'
     or coalesce(p_story_image_url, '') !~ '^https://'
     or coalesce(p_story_sha256, '') !~ '^[0-9a-f]{64}$'
     or coalesce(p_source_hash, '') !~ '^[0-9a-f]{64}$'
     or coalesce(p_artifact_tenant, '') <> 'lasso_ig'
     or nullif(p_policy_version, '') is null
     or p_brain_snapshot is null or p_brain_snapshot = '{}'::jsonb
     or p_story_scheduled_at is null then
    return jsonb_build_object('result','conflict','reason','invalid_input');
  end if;

  select * into m
    from public.lasso_standalone_story_conversion_manifest_20261008
   where story_id = p_story_id;
  if not found then
    return jsonb_build_object('result','conflict','reason','unknown_story');
  end if;
  story_input := public.lasso_conversion_snapshot_20261008(p_expected_story);
  feed_input := public.lasso_conversion_snapshot_20261008(p_expected_feed);
  if story_input is null or feed_input is null then
    return jsonb_build_object('result','conflict','reason','invalid_snapshot_timestamp');
  end if;
  -- Stale or forged before-state proof never converts; preserve all JSON types.
  if story_input is distinct from public.lasso_conversion_snapshot_20261008(m.expected_story) then
    return jsonb_build_object('result','conflict','reason','stale_story_preimage');
  end if;
  -- The Story may only bind its evidenced existing feed UUID.
  if p_feed_id is distinct from m.feed_id then
    return jsonb_build_object('result','conflict','reason','feed_mismatch');
  end if;

  -- Tenant lock first, then fresh authoritative autonomy, then row locks.
  perform pg_advisory_xact_lock(hashtextextended('lasso', 0));
  if public.calendar_gym_is_autonomous('lasso') is not true then
    return jsonb_build_object('result','conflict','reason','autonomy_not_confirmed');
  end if;

  select * into s from public.content_calendar
   where id = p_story_id for update;
  if not found then
    return jsonb_build_object('result','conflict','reason','story_missing');
  end if;
  select * into f from public.content_calendar
   where id = p_feed_id for update;
  if not found then
    return jsonb_build_object('result','conflict','reason','feed_missing');
  end if;

  -- Replay (checked only after tenant lock, fresh autonomy and row locks):
  -- identical input preimages and artifact plus the exact post-conversion
  -- Story snapshot, passing current-source row, registry link and current
  -- feed. Any changed source/state/autonomy or concurrent payload conflicts.
  select * into a
    from public.lasso_standalone_story_conversion_audit_20261008
   where story_id = p_story_id;
  if found then
    replay := true;
    if a.feed_id is distinct from p_feed_id
       or a.new_image_url is distinct from p_story_image_url
       or a.new_image_sha256 is distinct from p_story_sha256
       or a.artifact_tenant is distinct from p_artifact_tenant
       or a.source_hash is distinct from p_source_hash
       or a.policy_version is distinct from p_policy_version
       or a.brain_snapshot is distinct from p_brain_snapshot
       or a.story_scheduled_at is distinct from p_story_scheduled_at
       or story_input is distinct from public.lasso_conversion_snapshot_20261008(a.before_story)
       or feed_input is distinct from public.lasso_conversion_snapshot_20261008(a.before_feed)
       or public.lasso_conversion_snapshot_20261008(to_jsonb(s)) is distinct from
          public.lasso_conversion_snapshot_20261008(a.after_story)
       or public.lasso_conversion_snapshot_20261008(to_jsonb(f)) is distinct from
          public.lasso_conversion_snapshot_20261008(a.before_feed)
       or not exists (select 1 from public.lasso_managed_paired_stories ml
                       where ml.story_id = a.story_id and ml.feed_id = a.feed_id)
       or public.lasso_story_current_source(s.id) is not true then
      return jsonb_build_object('result','conflict','reason','replay_changed');
    end if;
  end if;

  -- Fresh conversion: exact eligible old NULL-slot state. Managed-registry
  -- absence is asserted BEFORE the full-preimage compare (the manifest
  -- preimage carries the complete production key set minus only the
  -- diagnostic managed_links field).
  if not replay then
    if s.gym_id is distinct from 'lasso'
       or lower(btrim(coalesce(s.account, ''))) is distinct from m.account
       or lower(btrim(coalesce(s.format, 'feed'))) is distinct from 'story'
       or s.post_date is distinct from m.post_date
       or s.slot_index is not null or s.logical_post_id is not null
       or s.scheduled_at is not null
       or s.status is distinct from 'pending'
       or s.variant_status is distinct from 'active'
       or s.published_at is not null or s.late_post_id is not null
       or s.publish_claim_token is not null
       or s.publish_reservation_day is not null then
      return jsonb_build_object('result','conflict','reason','story_not_convertible');
    end if;
    if exists (select 1 from public.lasso_managed_paired_stories ml
                where ml.story_id = s.id) then
      return jsonb_build_object('result','conflict','reason','story_already_managed');
    end if;
    -- Full-key-set story CAS: missing OR extra fields refuse; typed-timestamp
    -- canonicalization happens only after key validation.
    if (select coalesce(string_agg(kk, ',' order by kk), '')
          from jsonb_object_keys(m.expected_story) kk) is distinct from
       (select coalesce(string_agg(kk, ',' order by kk), '')
          from jsonb_object_keys(to_jsonb(s)) kk) then
      return jsonb_build_object('result','conflict','reason','story_key_mismatch');
    end if;
    if public.lasso_conversion_snapshot_20261008(to_jsonb(s)) is distinct from story_input then
      return jsonb_build_object('result','conflict','reason','story_changed_or_claimed');
    end if;
  end if; -- fresh Story preimage gates

  -- Per-account/day serialization before sibling scans.
  perform pg_advisory_xact_lock(hashtextextended(
    'lasso|' || m.account || '|' || m.post_date::text, 0));

  -- The locked feed must be the unique canonical slot-2 Summit feed on the
  -- same account/date, fully ready: NON-NULL pending/approved status, no
  -- claim/reservation/receipt, no media hold, real caption and https image.
  if (
    select count(*) from public.content_calendar c
     where c.gym_id = 'lasso' and c.post_date = m.post_date
       and lower(btrim(coalesce(c.account, ''))) = m.account
       and lower(btrim(coalesce(c.format, 'feed'))) = 'feed'
       and c.slot_index = 2 and c.variant_status = 'active'
  ) <> 1 then
    return jsonb_build_object('result','conflict','reason','ambiguous_feed_slot');
  end if;
  if f.gym_id is distinct from 'lasso'
     or lower(btrim(coalesce(f.account, ''))) is distinct from m.account
     or f.post_date is distinct from m.post_date
     or f.slot_index is distinct from 2
     or lower(btrim(coalesce(f.format, 'feed'))) is distinct from 'feed'
     or f.variant_status is distinct from 'active'
     or lower(btrim(coalesce(f.pillar, ''))) is distinct from 'summit'
     or f.status is null or f.status not in ('pending','approved')
     or f.published_at is not null or f.late_post_id is not null
     or f.publish_claim_token is not null
     or f.publish_reservation_day is not null
     or f.approval_kind is not null or f.approved_by is not null
     or f.approved_at is not null or f.approval_digest is not null
     or f.media_not_ready_reason is not null
     or nullif(btrim(coalesce(f.caption, '')), '') is null
     or coalesce(f.image_url, '') !~ '^https://'
     or f.scheduled_at is null
     or p_story_image_url = f.image_url then
    return jsonb_build_object('result','conflict','reason','feed_not_ready');
  end if;
  -- Full-key-set feed CAS against the locked row: missing OR extra fields
  -- refuse; typed-timestamp canonicalization only after key validation.
  if (select coalesce(string_agg(kk, ',' order by kk), '')
        from jsonb_object_keys(p_expected_feed) kk) is distinct from
     (select coalesce(string_agg(kk, ',' order by kk), '')
        from jsonb_object_keys(to_jsonb(f)) kk) then
    return jsonb_build_object('result','conflict','reason','feed_key_mismatch');
  end if;
  if public.lasso_conversion_snapshot_20261008(to_jsonb(f)) is distinct from feed_input then
    return jsonb_build_object('result','conflict','reason','feed_changed_or_pairing_mismatch');
  end if;
  -- Canonical schedule (DST-safe via zone arithmetic): feed exactly noon and
  -- Story exactly 12:15 in America/New_York on the post date.
  if (f.scheduled_at at time zone 'America/New_York')::date <> m.post_date
     or (f.scheduled_at at time zone 'America/New_York')::time <> time '12:00'
     or p_story_scheduled_at <> f.scheduled_at + interval '15 minutes'
     or (p_story_scheduled_at at time zone 'America/New_York')::date <> m.post_date
     or (p_story_scheduled_at at time zone 'America/New_York')::time <> time '12:15' then
    return jsonb_build_object('result','conflict','reason','noncanonical_schedule');
  end if;
  -- Fresh caption-bound PASS artwork for the CURRENT feed image: current
  -- Brain/policy, exact digest, same caption hash the Story artifact binds.
  if not exists (
    select 1 from public.echo_infographic_artifacts fa
     where fa.tenant = 'lasso_ig'
       and fa.image_url = f.image_url
       and fa.image_sha256 ~ '^[0-9a-f]{64}$'
       and fa.evidence->>'grade_status' = 'PASS'
       and nullif(btrim(fa.evidence->>'review_response_id'), '') is not null
       and fa.evidence->>'image_sha256' = fa.image_sha256
       and fa.evidence->>'policy_version' = p_policy_version
       and fa.evidence->'brain_snapshot' = p_brain_snapshot
       and fa.source_identity->>'source_id' =
           'content_calendar:' || f.id::text || ':caption'
       and fa.source_identity->>'source_hash' =
           encode(sha256(convert_to(f.caption, 'UTF8')), 'hex')
  ) then
    return jsonb_build_object('result','conflict','reason','feed_review_missing');
  end if;

  -- Legitimate canonical Stories in slots 0/1 coexist; refuse only same-slot-2
  -- or unknown/malformed-slot Stories on this account/date.
  if exists (
    select 1 from public.content_calendar c
     where c.id <> s.id and c.gym_id = 'lasso' and c.post_date = m.post_date
       and lower(btrim(coalesce(c.account, ''))) = m.account
       and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
       and coalesce(c.variant_status, 'active') = 'active'
       and coalesce(c.status, 'pending') not in ('denied','killed','failed')
       and (c.slot_index is null or c.slot_index not in (0,1,2) or c.slot_index = 2)
  ) then
    return jsonb_build_object('result','conflict','reason','competing_story_present');
  end if;
  -- The new artwork is not already bound to another active LASSO Story.
  if exists (
    select 1 from public.content_calendar c
     where c.id <> s.id and c.gym_id = 'lasso'
       and lower(btrim(coalesce(c.format, 'feed'))) = 'story'
       and coalesce(c.variant_status, 'active') = 'active'
       and c.status in ('pending','approved','publishing')
       and c.image_url = p_story_image_url
  ) then
    return jsonb_build_object('result','conflict','reason','story_media_already_in_use');
  end if;

  -- Fresh reviewed Story artifact: current Brain/policy PASS, exact hash,
  -- real response id, measured 9:16, bound to the feed's CURRENT caption only.
  if not exists (
    select 1 from public.echo_infographic_artifacts art
     where art.tenant = 'lasso_ig'
       and art.image_url = p_story_image_url
       and art.image_sha256 = p_story_sha256
       and art.evidence->>'grade_status' = 'PASS'
       and art.evidence->>'image_sha256' = p_story_sha256
       and art.evidence->>'policy_version' = p_policy_version
       and art.evidence->'brain_snapshot' = p_brain_snapshot
       and nullif(btrim(art.evidence->>'review_response_id'), '') is not null
       and art.evidence->>'aspect' = '9:16'
       and art.evidence->>'pixels' = '1080x1920'
       and art.evidence->'verified_dimensions'->>'width' = '1080'
       and art.evidence->'verified_dimensions'->>'height' = '1920'
       and art.evidence->'verified_dimensions'->>'image_sha256' = p_story_sha256
       and art.source_identity->>'source_id' =
           'content_calendar:' || f.id::text || ':caption'
       and art.source_identity->>'source_hash' = p_source_hash
       and p_source_hash = encode(sha256(convert_to(f.caption, 'UTF8')), 'hex')
  ) or not public.lasso_story_review_matches(f.id, p_story_image_url) then
    return jsonb_build_object('result','conflict','reason','reviewed_source_mismatch');
  end if;

  if replay then
    return jsonb_build_object('result','idempotent','id',a.story_id,'feed_id',a.feed_id);
  end if;

  -- Atomic conversion: preserve UUID/created_at; set slot 2, feed identity
  -- (NULL preserved), empty publish caption, current pillar, new
  -- image/source, canonical 12:15 schedule and the paired-feed hold.
  update public.content_calendar
     set slot_index = 2,
         logical_post_id = f.logical_post_id,
         caption = '',
         pillar = f.pillar,
         image_url = p_story_image_url,
         source_media_url = p_story_image_url,
         thumbnail_url = p_story_image_url,
         scheduled_at = p_story_scheduled_at,
         media_not_ready_reason = 'paired_feed_not_ready'
   where id = s.id;
  if not public.lasso_story_current_source(s.id) then
    raise exception 'converted Story failed source verification'
      using errcode = '23514';
  end if;
  insert into public.lasso_managed_paired_stories(story_id, feed_id)
  values (s.id, f.id)
  on conflict (story_id) do nothing;
  if not exists (select 1 from public.lasso_managed_paired_stories ml
                  where ml.story_id = s.id and ml.feed_id = f.id) then
    raise exception 'managed Story bound to another feed'
      using errcode = '23514';
  end if;
  insert into public.lasso_standalone_story_conversion_audit_20261008
    (story_id, feed_id, before_story, before_feed, after_story, new_image_url,
     new_image_sha256, artifact_tenant, source_hash, policy_version,
     brain_snapshot, story_scheduled_at, logical_post_id, hold_reason)
  values
    (s.id, f.id, to_jsonb(s), to_jsonb(f),
     (select to_jsonb(c) from public.content_calendar c where c.id=s.id), p_story_image_url,
     p_story_sha256, p_artifact_tenant, p_source_hash, p_policy_version,
     p_brain_snapshot, p_story_scheduled_at, f.logical_post_id,
     'paired_feed_not_ready');
  return jsonb_build_object('result','converted','id',s.id,'feed_id',f.id,
                            'hold_reason','paired_feed_not_ready');
end;
$$;

revoke all on table public.lasso_standalone_story_conversion_manifest_20261008
  from public, anon, authenticated, service_role;
revoke all on table public.lasso_standalone_story_conversion_audit_20261008
  from public, anon, authenticated, service_role;
revoke all on function public.lasso_conversion_snapshot_20261008(jsonb)
  from public, anon, authenticated, service_role;
revoke all on function public.lasso_story_conversion_immutable_20261008()
  from public, anon, authenticated;
revoke all on function public.convert_lasso_standalone_story_20261008(
  uuid,uuid,jsonb,jsonb,text,text,text,text,text,jsonb,timestamptz)
  from public, anon, authenticated;
grant execute on function public.convert_lasso_standalone_story_20261008(
  uuid,uuid,jsonb,jsonb,text,text,text,text,text,jsonb,timestamptz)
  to service_role;
commit;
