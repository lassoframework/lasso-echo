-- Owned atomic preparation of the five evidenced existing November LASSO
-- Summit Instagram feeds (Nov 1/2/4/5/6) while their matching standalone
-- Stories remain byte-identical. Service-role-only manifest RPC plus a
-- reviewed immutable manifest and an append-only immutable audit table.
--
-- Caption disposition (native independent Astra source review PASS,
-- astra-acceptance.md "Five November feed fact review and atomic preparation
-- contract", 2026-10-08T23:43Z): preserve Nov 1/2/4/6 captions byte-for-byte;
-- replace ONLY the Nov 5 unsupported 'Seats are selling fast' scarcity with
-- the exact reviewed corrective caption (nov5-approved-corrective-caption.json,
-- sha256 bed97dac7b41b5e3409761dbe4cebe797211ac127376c35c5e5bba1aa4fdc7ed;
-- the earlier dated-catalog candidate sha e5e72f67... is strictly BLOCKED).
-- No fabricated writer or API
-- response IDs exist for the text decision: the native independent review,
-- frozen catalog/brief SHAs and review policy below are the proof. The
-- candidate caption must exactly equal the manifest-allowed value; the unsafe
-- Nov 5 original passes only as the exact corrective TARGET preimage, never
-- as a candidate.
--
-- Every write requires: literal source-catalog SHA equality -> tenant
-- advisory lock -> fresh authoritative autonomy -> exact account/day advisory
-- lock -> short global content_calendar EXCLUSIVE write fence (blocks
-- phantom INSERTs and row-locking reads; ordinary reads still pass) ->
-- complete protected-book
-- (Oct 8 - Nov 8, all statuses/variants) snapshot read and exact normalized
-- CAS
-- -> exact feed and Story row locks -> full actual key-set and typed
-- full-snapshot CAS of BOTH rows against the manifest preimages (JSON null or
-- explicitly aware timestamp strings only; naive/empty/coerced values refuse)
-- -> eligible pending/active feed with the known cross-date hold, NULL
-- schedule/logical, no claims/reservation/receipts and all four approval
-- fields NULL -> standalone Story untouched and absent from the managed
-- registry -> unique slot-2 occupant -> NEW image HTTPS, distinct from the
-- old image and unused by any other active LASSO row -> a current
-- caption-bound PASS artifact for the NEW feed image (tenant lasso_ig, exact
-- feed UUID source identity, NEW caption hash, non-blank policy/Brain/review
-- receipt, exact image SHA and actual measured pixels: JSON-number
-- verified_dimensions 1080x1350, aspect exactly 4:5, pixels exactly
-- 1080x1350 and the nested verified_dimensions.image_sha256 digest, per the
-- root dimension attestation for this fixed set of five) -> canonical future
-- noon America/New_York schedule computed from the actual clock (DST-safe
-- zone arithmetic, no fake clock). Only the allow-listed feed
-- caption/media/thumbnail/asset/hold/schedule fields and (Nov 5 only) the
-- restamped learning levers change; the complete 42-column after-feed is
-- captured via RETURNING into the immutable audit, and idempotent replay
-- re-verifies that full after-feed, the byte-identical Story and every
-- registry/sibling/source/canonical proof under fresh locks/autonomy.
-- Root owns production application and live verification.
begin;

-- Append-only guard shared by the manifest and the audit table.
create or replace function public.lasso_feed_preparation_immutable_20261008()
returns trigger language plpgsql
as $$
begin
  raise exception 'lasso feed preparation record immutable'
    using errcode = '23514';
end;
$$;

-- Normalize a complete JSON row snapshot for comparison without stringified
-- coercion: JSON types and the exact key-set are preserved as jsonb, and the
-- four timestamp columns are normalized to epoch seconds. A timestamp value
-- must be JSON null or a valid explicitly timezone-aware string; empty
-- strings, naive timestamps, non-string values, missing keys, invalid
-- datetimes and non-object input all make the whole snapshot NULL (which
-- fails every equality check). Explicit offsets/Z normalize to one instant.
create or replace function public.lasso_feed_prep_snapshot_20261008(p_snapshot jsonb)
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

-- Full-snapshot equality on normalized snapshots: exact key-set and typed
-- value match; any malformed/naive/empty timestamp or non-object input is
-- never equal.
create or replace function public.lasso_feed_prep_snapshot_equal_20261008(
  p_actual jsonb, p_expected jsonb
) returns boolean
language sql stable set search_path = public
as $$
  select public.lasso_feed_prep_snapshot_20261008(p_actual) is not null
     and public.lasso_feed_prep_snapshot_20261008(p_expected) is not null
     and public.lasso_feed_prep_snapshot_20261008(p_actual)
         = public.lasso_feed_prep_snapshot_20261008(p_expected)
$$;

-- Reviewed immutable manifest: the five exact existing feed UUIDs, their
-- matching standalone Story UUIDs, complete original preimages (read-only
-- production queries 2026-10-08T22:38Z/23:14Z), the allowed caption + SHA
-- per feed, the restamped lever values for the Nov 5 correction, the
-- reviewed caption/source catalog and brief SHAs and the native Astra
-- review policy.
create table public.lasso_feed_preparation_manifest_20261008 (
  feed_id uuid primary key,
  story_id uuid not null unique,
  account text not null,
  post_date date not null,
  action text not null,
  old_caption_sha256 text not null,
  allowed_caption text not null,
  allowed_caption_sha256 text not null,
  new_hook_family text not null,
  new_ask_type text not null,
  new_caption_len_band text not null,
  expected_feed jsonb not null,
  expected_story jsonb not null,
  catalog_sha256 text not null,
  brief_sha256 text not null,
  review_policy text not null,
  reviewed_at timestamptz not null default now()
);
insert into public.lasso_feed_preparation_manifest_20261008
  (feed_id, story_id, account, post_date, action,
   old_caption_sha256, allowed_caption, allowed_caption_sha256,
   new_hook_family, new_ask_type, new_caption_len_band,
   expected_feed, expected_story,
   catalog_sha256, brief_sha256, review_policy) values
 ('47353415-893c-5372-954a-66121900a064','1d591c59-32c1-48a8-963f-ad1bce32ad00','instagram',date '2026-11-01',
  'preserve_exact',
  'e2e8ccad7cb98e41a57719d5e15ed8b512f207a7ae9775c300b5eca67a510e86',
  'Your gym does not need another vague goal. It needs a 2027 growth playbook built for the work ahead. Get tickets at lassoframework.com/summit',
  'e2e8ccad7cb98e41a57719d5e15ed8b512f207a7ae9775c300b5eca67a510e86',
  'bold_claim','none','short',
  '{"id":"47353415-893c-5372-954a-66121900a064","format":"feed","gym_id":"lasso","pillar":"summit","status":"pending","account":"instagram","caption":"Your gym does not need another vague goal. It needs a 2027 growth playbook built for the work ahead. Get tickets at lassoframework.com/summit","ask_type":"none","event_id":null,"mentions":[],"gbp_event":null,"gbp_offer":null,"image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso/cf568307cc2551ea/summitpeople-2026-10-29.png","post_date":"2026-11-01","time_slot":"evening","created_at":"2026-09-23T20:03:22.067242+00:00","slot_index":2,"variant_of":null,"approved_at":null,"approved_by":null,"gbp_cta_url":null,"hook_family":"bold_claim","gbp_cta_type":null,"late_post_id":null,"published_at":null,"scheduled_at":null,"approval_kind":null,"reject_reason":null,"thumbnail_url":null,"gbp_topic_type":null,"variant_status":"active","approval_digest":null,"gbp_location_id":null,"has_member_face":null,"logical_post_id":null,"caption_len_band":"short","experiment_label":null,"source_media_url":null,"publish_claim_token":null,"source_media_asset_id":null,"media_not_ready_reason":"cross_date_media_repeat_needs_new_visual","publish_reservation_day":null}'::jsonb,
  '{"id":"1d591c59-32c1-48a8-963f-ad1bce32ad00","gym_id":"lasso","account":"instagram","post_date":"2026-11-01","pillar":"summit","format":"story","caption":"","image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/0eb19dca872c513f/11.png","status":"pending","created_at":"2026-08-07 22:03:28.274168+00","published_at":null,"late_post_id":null,"scheduled_at":null,"thumbnail_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/0eb19dca872c513f/11.png","gbp_topic_type":null,"gbp_cta_type":null,"gbp_cta_url":null,"gbp_event":null,"gbp_offer":null,"gbp_location_id":null,"reject_reason":"","source_media_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/0eb19dca872c513f/11.png","mentions":[],"hook_family":"bold_claim","ask_type":"none","time_slot":"early_morning","caption_len_band":"short","has_member_face":null,"experiment_label":null,"slot_index":null,"source_media_asset_id":null,"media_not_ready_reason":null,"event_id":null,"variant_of":null,"variant_status":"active","publish_reservation_day":null,"publish_claim_token":null,"logical_post_id":null,"approval_kind":null,"approved_by":null,"approved_at":null,"approval_digest":null}'::jsonb,
  'f6424e0531e23cc2b1f0a9a6d990ba1698c713ccbb479bf649576605697daf3f',
  'f07045b51c82119684a0097f3d7ecc42c1d9eb262b715d6b861dfaa6b9a22644',
  'lasso-summit-source-correction-20261008-v1'),
 ('7e1ad844-1b18-5352-8287-8825fe54db99','47ea9c06-6d0e-45f3-a95f-20252b82cf03','instagram',date '2026-11-02',
  'preserve_exact',
  'a0868f2d81823b770775a291d271b70b07f69035398c995b66a663b56dec87a4',
  'A weekend of real strategy can create months of better decisions.

Meet us at Virgin Hotels Nashville on November 7 and 8. Register at lassoframework.com/summit',
  'a0868f2d81823b770775a291d271b70b07f69035398c995b66a663b56dec87a4',
  'bold_claim','none','mid',
  '{"id":"7e1ad844-1b18-5352-8287-8825fe54db99","format":"feed","gym_id":"lasso","pillar":"summit","status":"pending","account":"instagram","caption":"A weekend of real strategy can create months of better decisions.\n\nMeet us at Virgin Hotels Nashville on November 7 and 8. Register at lassoframework.com/summit","ask_type":"none","event_id":null,"mentions":[],"gbp_event":null,"gbp_offer":null,"image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso/9692e087c23e1611/summitpeople-2026-10-30.png","post_date":"2026-11-02","time_slot":null,"created_at":"2026-09-23T20:03:22.871485+00:00","slot_index":2,"variant_of":null,"approved_at":null,"approved_by":null,"gbp_cta_url":null,"hook_family":"bold_claim","gbp_cta_type":null,"late_post_id":null,"published_at":null,"scheduled_at":null,"approval_kind":null,"reject_reason":null,"thumbnail_url":null,"gbp_topic_type":null,"variant_status":"active","approval_digest":null,"gbp_location_id":null,"has_member_face":null,"logical_post_id":null,"caption_len_band":"mid","experiment_label":null,"source_media_url":null,"publish_claim_token":null,"source_media_asset_id":null,"media_not_ready_reason":"cross_date_media_repeat_needs_new_visual","publish_reservation_day":null}'::jsonb,
  '{"id":"47ea9c06-6d0e-45f3-a95f-20252b82cf03","gym_id":"lasso","account":"instagram","post_date":"2026-11-02","pillar":"summit","format":"story","caption":"","image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/0eb19dca872c513f/11.png","status":"pending","created_at":"2026-08-07 22:03:28.274168+00","published_at":null,"late_post_id":null,"scheduled_at":null,"thumbnail_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/0eb19dca872c513f/11.png","gbp_topic_type":null,"gbp_cta_type":null,"gbp_cta_url":null,"gbp_event":null,"gbp_offer":null,"gbp_location_id":null,"reject_reason":"","source_media_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/0eb19dca872c513f/11.png","mentions":[],"hook_family":"bold_claim","ask_type":"none","time_slot":"early_morning","caption_len_band":"short","has_member_face":null,"experiment_label":null,"slot_index":null,"source_media_asset_id":null,"media_not_ready_reason":"cross_date_media_repeat_needs_new_visual","event_id":null,"variant_of":null,"variant_status":"active","publish_reservation_day":null,"publish_claim_token":null,"logical_post_id":null,"approval_kind":null,"approved_by":null,"approved_at":null,"approval_digest":null}'::jsonb,
  'f6424e0531e23cc2b1f0a9a6d990ba1698c713ccbb479bf649576605697daf3f',
  'f07045b51c82119684a0097f3d7ecc42c1d9eb262b715d6b861dfaa6b9a22644',
  'lasso-summit-source-correction-20261008-v1'),
 ('4db9ec0e-e762-5f8e-8a18-ad7ea56cad07','6df42648-2c7f-4522-bc6d-37a98eb8ad3d','instagram',date '2026-11-04',
  'preserve_exact',
  'f9613c764bc9bde85661057018abea76e390533207a2424a6546aecf67de859f',
  'This week in Nashville.

Bring the questions that deserve more than a quick answer and build the next plan with us. Get tickets at lassoframework.com/summit',
  'f9613c764bc9bde85661057018abea76e390533207a2424a6546aecf67de859f',
  'bold_claim','none','mid',
  '{"id":"4db9ec0e-e762-5f8e-8a18-ad7ea56cad07","format":"feed","gym_id":"lasso","pillar":"summit","status":"pending","account":"instagram","caption":"This week in Nashville.\n\nBring the questions that deserve more than a quick answer and build the next plan with us. Get tickets at lassoframework.com/summit","ask_type":"none","event_id":null,"mentions":[],"gbp_event":null,"gbp_offer":null,"image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso/c749f4f6a4de6c88/summitpeople-2026-11-01.png","post_date":"2026-11-04","time_slot":null,"created_at":"2026-09-23T20:03:24.538146+00:00","slot_index":2,"variant_of":null,"approved_at":null,"approved_by":null,"gbp_cta_url":null,"hook_family":"bold_claim","gbp_cta_type":null,"late_post_id":null,"published_at":null,"scheduled_at":null,"approval_kind":null,"reject_reason":null,"thumbnail_url":null,"gbp_topic_type":null,"variant_status":"active","approval_digest":null,"gbp_location_id":null,"has_member_face":null,"logical_post_id":null,"caption_len_band":"mid","experiment_label":null,"source_media_url":null,"publish_claim_token":null,"source_media_asset_id":null,"media_not_ready_reason":"cross_date_media_repeat_needs_new_visual","publish_reservation_day":null}'::jsonb,
  '{"id":"6df42648-2c7f-4522-bc6d-37a98eb8ad3d","gym_id":"lasso","account":"instagram","post_date":"2026-11-04","pillar":"summit","format":"story","caption":"","image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/8863e022991b4e64/16.png","status":"pending","created_at":"2026-08-07 22:03:28.274168+00","published_at":null,"late_post_id":null,"scheduled_at":null,"thumbnail_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/8863e022991b4e64/16.png","gbp_topic_type":null,"gbp_cta_type":null,"gbp_cta_url":null,"gbp_event":null,"gbp_offer":null,"gbp_location_id":null,"reject_reason":"","source_media_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/8863e022991b4e64/16.png","mentions":[],"hook_family":"bold_claim","ask_type":"none","time_slot":"early_morning","caption_len_band":"short","has_member_face":null,"experiment_label":null,"slot_index":null,"source_media_asset_id":null,"media_not_ready_reason":null,"event_id":null,"variant_of":null,"variant_status":"active","publish_reservation_day":null,"publish_claim_token":null,"logical_post_id":null,"approval_kind":null,"approved_by":null,"approved_at":null,"approval_digest":null}'::jsonb,
  'f6424e0531e23cc2b1f0a9a6d990ba1698c713ccbb479bf649576605697daf3f',
  'f07045b51c82119684a0097f3d7ecc42c1d9eb262b715d6b861dfaa6b9a22644',
  'lasso-summit-source-correction-20261008-v1'),
 ('96ef0beb-bd99-5358-bb75-820085ecc92e','53e0fa31-8676-4624-a877-673883859509','instagram',date '2026-11-05',
  'replace_unsupported_scarcity',
  '2e1521ed99da2e85d7564663c1e429b05da84ad81d4b8a9ce647f3d9a17390f0',
  'Give your gym a plan with enough detail to use on Monday.

Build your complete 2027 growth playbook at the LASSO Growth Summit on November 7 and 8 at Virgin Hotels Nashville.

Register at lassoframework.com/summit',
  'bed97dac7b41b5e3409761dbe4cebe797211ac127376c35c5e5bba1aa4fdc7ed',
  'bold_claim','none','mid',
  '{"id":"96ef0beb-bd99-5358-bb75-820085ecc92e","format":"feed","gym_id":"lasso","pillar":"summit","status":"pending","account":"instagram","caption":"Seats are selling fast.\n\nGet your tickets now. The calendar is about to turn. Give your gym a plan with enough detail to use on Monday. Register at lassoframework.com/summit","ask_type":"none","event_id":null,"mentions":[],"gbp_event":null,"gbp_offer":null,"image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso/b0a128ec5eb39980/summitpeople-2026-11-02.png","post_date":"2026-11-05","time_slot":null,"created_at":"2026-09-23T20:03:25.394509+00:00","slot_index":2,"variant_of":null,"approved_at":null,"approved_by":null,"gbp_cta_url":null,"hook_family":"bold_claim","gbp_cta_type":null,"late_post_id":null,"published_at":null,"scheduled_at":null,"approval_kind":null,"reject_reason":null,"thumbnail_url":null,"gbp_topic_type":null,"variant_status":"active","approval_digest":null,"gbp_location_id":null,"has_member_face":null,"logical_post_id":null,"caption_len_band":"mid","experiment_label":null,"source_media_url":null,"publish_claim_token":null,"source_media_asset_id":null,"media_not_ready_reason":"cross_date_media_repeat_needs_new_visual","publish_reservation_day":null}'::jsonb,
  '{"id":"53e0fa31-8676-4624-a877-673883859509","gym_id":"lasso","account":"instagram","post_date":"2026-11-05","pillar":"summit","format":"story","caption":"","image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/ceb33e0112d7eb80/20.png","status":"pending","created_at":"2026-08-07 22:03:28.274168+00","published_at":null,"late_post_id":null,"scheduled_at":null,"thumbnail_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/ceb33e0112d7eb80/20.png","gbp_topic_type":null,"gbp_cta_type":null,"gbp_cta_url":null,"gbp_event":null,"gbp_offer":null,"gbp_location_id":null,"reject_reason":"","source_media_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/ceb33e0112d7eb80/20.png","mentions":[],"hook_family":"bold_claim","ask_type":"none","time_slot":"early_morning","caption_len_band":"short","has_member_face":null,"experiment_label":null,"slot_index":null,"source_media_asset_id":null,"media_not_ready_reason":null,"event_id":null,"variant_of":null,"variant_status":"active","publish_reservation_day":null,"publish_claim_token":null,"logical_post_id":null,"approval_kind":null,"approved_by":null,"approved_at":null,"approval_digest":null}'::jsonb,
  'f6424e0531e23cc2b1f0a9a6d990ba1698c713ccbb479bf649576605697daf3f',
  'f07045b51c82119684a0097f3d7ecc42c1d9eb262b715d6b861dfaa6b9a22644',
  'lasso-summit-source-correction-20261008-v1'),
 ('df8aabea-1ddb-5b44-9b31-f57eb8bc8202','46a30cd8-cff1-42b4-82b4-ce2c8acc14cc','instagram',date '2026-11-06',
  'preserve_exact',
  '2a38b78b6612fb6666dcf0d3cd0489fa1a6367736c6e829089592b2213e95b82',
  'The LASSO Growth Summit puts serious operators in one place for two focused days.

Nashville is this Saturday and Sunday. Claim your seat at lassoframework.com/summit',
  '2a38b78b6612fb6666dcf0d3cd0489fa1a6367736c6e829089592b2213e95b82',
  'bold_claim','booking_link','mid',
  '{"id":"df8aabea-1ddb-5b44-9b31-f57eb8bc8202","format":"feed","gym_id":"lasso","pillar":"summit","status":"pending","account":"instagram","caption":"The LASSO Growth Summit puts serious operators in one place for two focused days.\n\nNashville is this Saturday and Sunday. Claim your seat at lassoframework.com/summit","ask_type":"booking_link","event_id":null,"mentions":[],"gbp_event":null,"gbp_offer":null,"image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso/986fa0781d47f19e/summitpeople-2026-11-03.png","post_date":"2026-11-06","time_slot":null,"created_at":"2026-09-23T20:03:26.149202+00:00","slot_index":2,"variant_of":null,"approved_at":null,"approved_by":null,"gbp_cta_url":null,"hook_family":"bold_claim","gbp_cta_type":null,"late_post_id":null,"published_at":null,"scheduled_at":null,"approval_kind":null,"reject_reason":null,"thumbnail_url":null,"gbp_topic_type":null,"variant_status":"active","approval_digest":null,"gbp_location_id":null,"has_member_face":null,"logical_post_id":null,"caption_len_band":"mid","experiment_label":null,"source_media_url":null,"publish_claim_token":null,"source_media_asset_id":null,"media_not_ready_reason":"cross_date_media_repeat_needs_new_visual","publish_reservation_day":null}'::jsonb,
  '{"id":"46a30cd8-cff1-42b4-82b4-ce2c8acc14cc","gym_id":"lasso","account":"instagram","post_date":"2026-11-06","pillar":"summit","format":"story","caption":"","image_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/8faf74d20266cc5b/18.png","status":"pending","created_at":"2026-08-07 22:03:28.274168+00","published_at":null,"late_post_id":null,"scheduled_at":null,"thumbnail_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/8faf74d20266cc5b/18.png","gbp_topic_type":null,"gbp_cta_type":null,"gbp_cta_url":null,"gbp_event":null,"gbp_offer":null,"gbp_location_id":null,"reject_reason":"","source_media_url":"https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/echo/lasso_ig/8faf74d20266cc5b/18.png","mentions":[],"hook_family":"bold_claim","ask_type":"none","time_slot":"early_morning","caption_len_band":"short","has_member_face":null,"experiment_label":null,"slot_index":null,"source_media_asset_id":null,"media_not_ready_reason":null,"event_id":null,"variant_of":null,"variant_status":"active","publish_reservation_day":null,"publish_claim_token":null,"logical_post_id":null,"approval_kind":null,"approved_by":null,"approved_at":null,"approval_digest":null}'::jsonb,
  'f6424e0531e23cc2b1f0a9a6d990ba1698c713ccbb479bf649576605697daf3f',
  'f07045b51c82119684a0097f3d7ecc42c1d9eb262b715d6b861dfaa6b9a22644',
  'lasso-summit-source-correction-20261008-v1');

-- Append-only immutable preparation receipt: full before-state plus the exact
-- applied values. No update/delete/truncate ever; not readable by app roles.
create table public.lasso_feed_preparation_audit_20261008 (
  feed_id uuid primary key,
  story_id uuid not null,
  prepared_at timestamptz not null default now(),
  post_date date not null,
  action text not null,
  caption_changed boolean not null,
  old_caption text not null,
  new_caption text not null,
  old_caption_sha256 text not null,
  new_caption_sha256 text not null,
  before_feed jsonb not null,
  before_story jsonb not null,
  after_feed jsonb not null,
  new_image_url text not null,
  new_image_sha256 text not null,
  artifact_tenant text not null,
  policy_version text not null,
  brain_snapshot jsonb not null,
  review_response_id text not null,
  scheduled_at timestamptz not null,
  catalog_sha256 text not null,
  brief_sha256 text not null,
  review_policy text not null
);

create trigger manifest_immutable_row before update or delete
  on public.lasso_feed_preparation_manifest_20261008
  for each row execute function public.lasso_feed_preparation_immutable_20261008();
create trigger manifest_immutable_truncate before truncate
  on public.lasso_feed_preparation_manifest_20261008
  for each statement execute function public.lasso_feed_preparation_immutable_20261008();
create trigger audit_immutable_row before update or delete
  on public.lasso_feed_preparation_audit_20261008
  for each row execute function public.lasso_feed_preparation_immutable_20261008();
create trigger audit_immutable_truncate before truncate
  on public.lasso_feed_preparation_audit_20261008
  for each statement execute function public.lasso_feed_preparation_immutable_20261008();

create or replace function public.prepare_lasso_summit_feed_20261008(
  p_feed_id uuid, p_story_id uuid,
  p_expected_feed jsonb, p_expected_story jsonb,
  p_caption text, p_caption_sha256 text,
  p_new_image_url text, p_new_image_sha256 text,
  p_artifact_tenant text, p_policy_version text,
  p_brain_snapshot jsonb, p_review_response_id text,
  p_expected_book jsonb, p_source_catalog_sha text
) returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
  m public.lasso_feed_preparation_manifest_20261008%rowtype;
  a public.lasso_feed_preparation_audit_20261008%rowtype;
  f public.content_calendar%rowtype;
  f_after public.content_calendar%rowtype;
  s public.content_calendar%rowtype;
  s_now public.content_calendar%rowtype;
  v_noon timestamptz;
  v_changed boolean;
  v_book jsonb;
  v_expected_book jsonb;
begin
  -- Strict input shape. Unknown/malformed values never reach a row lock. The
  -- protected book must be a JSON array of complete valid row snapshots (JSON
  -- types preserved; naive/empty/non-string timestamps refuse here).
  if p_feed_id is null or p_story_id is null or p_feed_id = p_story_id
     or jsonb_typeof(p_expected_feed) is distinct from 'object'
     or jsonb_typeof(p_expected_story) is distinct from 'object'
     or jsonb_typeof(p_expected_book) is distinct from 'array'
     or exists (select 1 from jsonb_array_elements(p_expected_book) e
                 where public.lasso_feed_prep_snapshot_20261008(e) is null)
     or nullif(p_caption, '') is null
     or coalesce(p_caption_sha256, '') !~ '^[0-9a-f]{64}$'
     or encode(sha256(convert_to(p_caption, 'UTF8')), 'hex') <> p_caption_sha256
     or coalesce(p_new_image_url, '') !~ '^https://'
     or coalesce(p_new_image_sha256, '') !~ '^[0-9a-f]{64}$'
     or coalesce(p_artifact_tenant, '') <> 'lasso_ig'
     or nullif(p_policy_version, '') is null
     or p_brain_snapshot is null or p_brain_snapshot = '{}'::jsonb
     or nullif(btrim(p_review_response_id), '') is null
     or coalesce(p_source_catalog_sha, '') !~ '^[0-9a-f]{64}$' then
    return jsonb_build_object('result','conflict','reason','invalid_input');
  end if;

  select * into m
    from public.lasso_feed_preparation_manifest_20261008
   where feed_id = p_feed_id;
  if not found then
    return jsonb_build_object('result','conflict','reason','unknown_feed');
  end if;
  -- The feed may only be prepared alongside its evidenced standalone Story.
  if p_story_id is distinct from m.story_id then
    return jsonb_build_object('result','conflict','reason','story_mismatch');
  end if;
  -- Literal source-catalog proof against the immutable manifest, before any
  -- write. The reviewed catalog SHA is not caller-negotiable.
  if p_source_catalog_sha is distinct from m.catalog_sha256 then
    return jsonb_build_object('result','conflict','reason','catalog_mismatch');
  end if;
  -- The candidate must exactly equal the manifest-allowed caption. The unsafe
  -- Nov 5 original is only the corrective TARGET preimage; it never passes
  -- here. No other text is accepted for any of the five feeds.
  if p_caption is distinct from m.allowed_caption
     or p_caption_sha256 is distinct from m.allowed_caption_sha256 then
    return jsonb_build_object('result','conflict','reason','caption_not_allowed');
  end if;
  -- Stale or forged before-state proof never prepares.
  if p_expected_feed is distinct from m.expected_feed then
    return jsonb_build_object('result','conflict','reason','stale_feed_preimage');
  end if;
  if p_expected_story is distinct from m.expected_story then
    return jsonb_build_object('result','conflict','reason','stale_story_preimage');
  end if;

  -- Lock order (fixed, matching ordinary Story writers which take the
  -- account/day advisory lock BEFORE any table/row lock, so there is no
  -- lock-order cycle): tenant advisory -> fresh authoritative autonomy ->
  -- exact account/day advisory -> short GLOBAL table write fence -> book
  -- snapshot read/CAS -> target row locks -> update -> commit releases.
  -- LOCK TABLE ... EXCLUSIVE permits ordinary reads but blocks inserts,
  -- row-locking reads and write upgrades for the brief duration of this
  -- DB-only RPC: FOR SHARE on existing rows alone could not stop phantom
  -- INSERTs (e.g. portal direct POST) landing after the book read. This is a
  -- short global table write fence, NOT tenant-only locking.
  perform pg_advisory_xact_lock(hashtextextended('lasso', 0));
  if public.calendar_gym_is_autonomous('lasso') is not true then
    return jsonb_build_object('result','conflict','reason','autonomy_not_confirmed');
  end if;
  perform pg_advisory_xact_lock(hashtextextended(
    'lasso|' || m.account || '|' || m.post_date::text, 0));
  lock table public.content_calendar in exclusive mode;

  -- Protected-book CAS: read the COMPLETE current LASSO calendar book (Oct 8
  -- - Nov 8, every status and variant) under the fence and compare the full
  -- normalized snapshot against the caller's exactly. The operator grades/
  -- checks history/Brain on its snapshot; this CAS proves the context did
  -- not drift before the write. Runs on both fresh and replay calls.
  select coalesce(jsonb_agg(public.lasso_feed_prep_snapshot_20261008(to_jsonb(b))
                            order by b.id), '[]'::jsonb)
    into v_book
    from public.content_calendar b
   where b.gym_id = 'lasso'
     and b.post_date between date '2026-10-08' and date '2026-11-08';
  select coalesce(jsonb_agg(public.lasso_feed_prep_snapshot_20261008(e)
                            order by e->>'id'), '[]'::jsonb)
    into v_expected_book
    from jsonb_array_elements(p_expected_book) e;
  if v_book is distinct from v_expected_book then
    return jsonb_build_object('result','conflict','reason','book_mismatch');
  end if;

  -- Exact feed and Story row locks.
  select * into f from public.content_calendar
   where id = p_feed_id for update;
  if not found then
    return jsonb_build_object('result','conflict','reason','feed_missing');
  end if;
  select * into s from public.content_calendar
   where id = p_story_id for update;
  if not found then
    return jsonb_build_object('result','conflict','reason','story_missing');
  end if;

  -- Canonical future noon America/New_York on the post date, computed with
  -- DST-safe zone arithmetic from the actual clock. Never a fake clock.
  v_noon := ((m.post_date::text || ' 12:00')::timestamp
             at time zone 'America/New_York');
  if (v_noon at time zone 'America/New_York')::date <> m.post_date
     or (v_noon at time zone 'America/New_York')::time <> time '12:00'
     or v_noon <= now() then
    return jsonb_build_object('result','conflict','reason','not_future_schedule');
  end if;

  -- Replay (checked only after tenant lock, fresh autonomy, book CAS and row
  -- locks): exact recorded inputs/preimages, the FULL normalized current feed
  -- equal to the audited after-feed (no selected-field shortcut), a
  -- byte-identical standalone Story, and re-run registry/sibling/source/
  -- canonical proofs. Any changed state/source/input conflicts.
  select * into a
    from public.lasso_feed_preparation_audit_20261008
   where feed_id = p_feed_id;
  if found then
    if a.story_id = p_story_id
       and a.new_caption = p_caption
       and a.new_caption_sha256 = p_caption_sha256
       and a.new_image_url = p_new_image_url
       and a.new_image_sha256 = p_new_image_sha256
       and a.artifact_tenant = p_artifact_tenant
       and a.policy_version = p_policy_version
       and a.brain_snapshot = p_brain_snapshot
       and a.review_response_id = p_review_response_id
       and a.catalog_sha256 = p_source_catalog_sha
       and public.lasso_feed_prep_snapshot_equal_20261008(
             a.before_feed, m.expected_feed)
       and public.lasso_feed_prep_snapshot_equal_20261008(
             a.before_story, m.expected_story)
       and a.scheduled_at = v_noon
       and public.lasso_feed_prep_snapshot_equal_20261008(
             to_jsonb(f), a.after_feed)
       and public.lasso_feed_prep_snapshot_equal_20261008(
             to_jsonb(s), a.before_story)
       and not exists (select 1 from public.lasso_managed_paired_stories ml
                        where ml.story_id = s.id or ml.feed_id = f.id)
       and (
         select count(*) from public.content_calendar c
          where c.gym_id = 'lasso' and c.post_date = m.post_date
            and lower(btrim(coalesce(c.account, ''))) = m.account
            and lower(btrim(coalesce(c.format, 'feed'))) = 'feed'
            and c.slot_index = 2 and c.variant_status = 'active'
       ) = 1
       and not exists (
         select 1 from public.content_calendar c
          where c.id <> f.id and c.gym_id = 'lasso'
            and coalesce(c.variant_status, 'active') = 'active'
            and coalesce(c.status, 'pending') not in ('denied','killed','failed')
            and (c.image_url = p_new_image_url
                 or c.source_media_url = p_new_image_url)
       )
       and exists (
         select 1 from public.echo_infographic_artifacts art
          where art.tenant = 'lasso_ig'
            and art.image_url = p_new_image_url
            and art.image_sha256 = p_new_image_sha256
            and art.evidence->>'grade_status' = 'PASS'
            and art.evidence->>'image_sha256' = p_new_image_sha256
            and art.evidence->>'policy_version' = p_policy_version
            and art.evidence->'brain_snapshot' = p_brain_snapshot
            and nullif(btrim(art.evidence->>'review_response_id'), '') is not null
            and art.evidence->>'review_response_id' = p_review_response_id
            and jsonb_typeof(art.evidence->'verified_dimensions'->'width') = 'number'
            and (art.evidence->'verified_dimensions'->>'width')::numeric = 1080
            and jsonb_typeof(art.evidence->'verified_dimensions'->'height') = 'number'
            and (art.evidence->'verified_dimensions'->>'height')::numeric = 1350
            and art.evidence->>'aspect' = '4:5'
            and art.evidence->>'pixels' = '1080x1350'
            and art.evidence->'verified_dimensions'->>'image_sha256' = p_new_image_sha256
            and art.source_identity->>'source_id' =
                'content_calendar:' || f.id::text || ':caption'
            and art.source_identity->>'source_hash' = p_caption_sha256
       ) then
      return jsonb_build_object('result','idempotent','id',a.feed_id,
                                'story_id',a.story_id,
                                'caption_changed',a.caption_changed);
    end if;
    return jsonb_build_object('result','conflict','reason','replay_changed');
  end if;

  -- Feed eligibility: exact manifest identity, pending + variant-active, the
  -- known cross-date hold, NULL schedule/logical, no claims, reservation,
  -- publish receipts or any of the four approval fields, https image present.
  if f.gym_id is distinct from 'lasso'
     or lower(btrim(coalesce(f.account, ''))) is distinct from m.account
     or f.post_date is distinct from m.post_date
     or f.slot_index is distinct from 2
     or lower(btrim(coalesce(f.format, 'feed'))) is distinct from 'feed'
     or lower(btrim(coalesce(f.pillar, ''))) is distinct from 'summit'
     or f.status is distinct from 'pending'
     or f.variant_status is distinct from 'active'
     or f.published_at is not null or f.late_post_id is not null
     or f.publish_claim_token is not null
     or f.publish_reservation_day is not null
     or f.approval_kind is not null or f.approved_by is not null
     or f.approved_at is not null or f.approval_digest is not null
     or f.logical_post_id is not null
     or f.scheduled_at is not null
     or f.media_not_ready_reason is distinct from
        'cross_date_media_repeat_needs_new_visual'
     or coalesce(f.image_url, '') !~ '^https://'
     or p_new_image_url = f.image_url
     or encode(sha256(convert_to(coalesce(f.caption, ''), 'UTF8')), 'hex')
        is distinct from m.old_caption_sha256 then
    return jsonb_build_object('result','conflict','reason','feed_not_ready');
  end if;
  -- Story eligibility: exact standalone state (NULL slot/logical/schedule,
  -- pending, unclaimed, no receipts) and no managed-registry link on either
  -- side. The Story itself is never modified.
  if s.gym_id is distinct from 'lasso'
     or lower(btrim(coalesce(s.account, ''))) is distinct from m.account
     or s.post_date is distinct from m.post_date
     or lower(btrim(coalesce(s.format, 'feed'))) is distinct from 'story'
     or s.slot_index is not null or s.logical_post_id is not null
     or s.scheduled_at is not null
     or s.status is distinct from 'pending'
     or s.variant_status is distinct from 'active'
     or s.published_at is not null or s.late_post_id is not null
     or s.publish_claim_token is not null
     or s.publish_reservation_day is not null then
    return jsonb_build_object('result','conflict','reason','story_not_eligible');
  end if;
  if exists (select 1 from public.lasso_managed_paired_stories ml
              where ml.story_id = s.id or ml.feed_id = f.id) then
    return jsonb_build_object('result','conflict','reason','registry_link_present');
  end if;

  -- Full-key-set typed CAS of BOTH locked rows against the manifest
  -- preimages: missing OR extra fields refuse.
  if not public.lasso_feed_prep_snapshot_equal_20261008(
           to_jsonb(f), m.expected_feed) then
    return jsonb_build_object('result','conflict','reason','feed_changed_or_claimed');
  end if;
  if not public.lasso_feed_prep_snapshot_equal_20261008(
           to_jsonb(s), m.expected_story) then
    return jsonb_build_object('result','conflict','reason','story_changed_or_claimed');
  end if;

  -- Per-account/day advisory lock already held (acquired before the table
  -- fence); the occupancy scan is serialized by it plus the fence.

  -- Unique canonical slot-2 active feed occupant on this account/date.
  if (
    select count(*) from public.content_calendar c
     where c.gym_id = 'lasso' and c.post_date = m.post_date
       and lower(btrim(coalesce(c.account, ''))) = m.account
       and lower(btrim(coalesce(c.format, 'feed'))) = 'feed'
       and c.slot_index = 2 and c.variant_status = 'active'
  ) <> 1 then
    return jsonb_build_object('result','conflict','reason','ambiguous_feed_slot');
  end if;

  -- The NEW image must not be reused by another active LASSO calendar row
  -- (cross-date reuse conflict).
  if exists (
    select 1 from public.content_calendar c
     where c.id <> f.id and c.gym_id = 'lasso'
       and coalesce(c.variant_status, 'active') = 'active'
       and coalesce(c.status, 'pending') not in ('denied','killed','failed')
       and (c.image_url = p_new_image_url
            or c.source_media_url = p_new_image_url)
  ) then
    return jsonb_build_object('result','conflict','reason','image_already_in_use');
  end if;

  -- Current PASS artifact for the NEW feed image: tenant lasso_ig, exact feed
  -- UUID source identity bound to the NEW caption hash, non-blank current
  -- policy/Brain, exact image SHA, the actual non-blank review receipt and
  -- actual measured pixels (real width/height consistent with the pixels
  -- field, not a declared-only aspect).
  if not exists (
    select 1 from public.echo_infographic_artifacts art
     where art.tenant = 'lasso_ig'
       and art.image_url = p_new_image_url
       and art.image_sha256 = p_new_image_sha256
       and art.evidence->>'grade_status' = 'PASS'
       and art.evidence->>'image_sha256' = p_new_image_sha256
       and art.evidence->>'policy_version' = p_policy_version
       and art.evidence->'brain_snapshot' = p_brain_snapshot
       and nullif(btrim(art.evidence->>'review_response_id'), '') is not null
       and art.evidence->>'review_response_id' = p_review_response_id
       and jsonb_typeof(art.evidence->'verified_dimensions'->'width') = 'number'
       and (art.evidence->'verified_dimensions'->>'width')::numeric = 1080
       and jsonb_typeof(art.evidence->'verified_dimensions'->'height') = 'number'
       and (art.evidence->'verified_dimensions'->>'height')::numeric = 1350
       and art.evidence->>'aspect' = '4:5'
       and art.evidence->>'pixels' = '1080x1350'
       and art.evidence->'verified_dimensions'->>'image_sha256' = p_new_image_sha256
       and art.source_identity->>'source_id' =
           'content_calendar:' || f.id::text || ':caption'
       and art.source_identity->>'source_hash' = p_caption_sha256
  ) then
    return jsonb_build_object('result','conflict','reason','image_review_missing');
  end if;

  -- Atomic allow-listed update of the feed only, capturing the complete
  -- 42-column after-row via RETURNING for the immutable audit. When the
  -- caption changes (Nov 5 correction), restamp the learning levers to the
  -- reviewed values; the length band is re-derived in SQL from the exact new
  -- caption.
  v_changed := f.caption is distinct from p_caption;
  if v_changed then
    if (case when char_length(p_caption) < 150 then 'short'
             when char_length(p_caption) <= 500 then 'mid'
             else 'long' end) is distinct from m.new_caption_len_band then
      raise exception 'manifest lever band mismatch for corrective caption'
        using errcode = '23514';
    end if;
    update public.content_calendar
       set caption = p_caption,
           hook_family = m.new_hook_family,
           ask_type = m.new_ask_type,
           caption_len_band = m.new_caption_len_band,
           image_url = p_new_image_url,
           source_media_url = p_new_image_url,
           thumbnail_url = null,
           source_media_asset_id = null,
           media_not_ready_reason = null,
           scheduled_at = v_noon
     where id = f.id
     returning * into f_after;
  else
    update public.content_calendar
       set image_url = p_new_image_url,
           source_media_url = p_new_image_url,
           thumbnail_url = null,
           source_media_asset_id = null,
           media_not_ready_reason = null,
           scheduled_at = v_noon
     where id = f.id
     returning * into f_after;
  end if;

  -- Post-update verification: the standalone Story must remain byte-identical
  -- to its locked preimage. Any drift raises and rolls the whole
  -- transaction back (no partial preparation, no audit row).
  select * into s_now from public.content_calendar where id = s.id;
  if not public.lasso_feed_prep_snapshot_equal_20261008(
           to_jsonb(s_now), m.expected_story) then
    raise exception 'standalone Story changed during feed preparation'
      using errcode = '23514';
  end if;

  insert into public.lasso_feed_preparation_audit_20261008
    (feed_id, story_id, post_date, action, caption_changed,
     old_caption, new_caption, old_caption_sha256, new_caption_sha256,
     before_feed, before_story, after_feed, new_image_url, new_image_sha256,
     artifact_tenant, policy_version, brain_snapshot, review_response_id,
     scheduled_at, catalog_sha256, brief_sha256, review_policy)
  values
    (f.id, s.id, m.post_date, m.action, v_changed,
     f.caption, p_caption, m.old_caption_sha256, m.allowed_caption_sha256,
     to_jsonb(f), to_jsonb(s), to_jsonb(f_after), p_new_image_url,
     p_new_image_sha256,
     p_artifact_tenant, p_policy_version, p_brain_snapshot, p_review_response_id,
     v_noon, m.catalog_sha256, m.brief_sha256, m.review_policy);
  return jsonb_build_object('result','prepared','id',f.id,'story_id',s.id,
                            'caption_changed',v_changed,
                            'scheduled_at',v_noon);
end;
$$;

revoke all on table public.lasso_feed_preparation_manifest_20261008
  from public, anon, authenticated, service_role;
revoke all on table public.lasso_feed_preparation_audit_20261008
  from public, anon, authenticated, service_role;
revoke all on function public.lasso_feed_preparation_immutable_20261008()
  from public, anon, authenticated;
revoke all on function public.lasso_feed_prep_snapshot_20261008(jsonb)
  from public, anon, authenticated;
revoke all on function public.lasso_feed_prep_snapshot_equal_20261008(jsonb,jsonb)
  from public, anon, authenticated;
revoke all on function public.prepare_lasso_summit_feed_20261008(
  uuid,uuid,jsonb,jsonb,text,text,text,text,text,text,jsonb,text,jsonb,text)
  from public, anon, authenticated;
grant execute on function public.prepare_lasso_summit_feed_20261008(
  uuid,uuid,jsonb,jsonb,text,text,text,text,text,text,jsonb,text,jsonb,text)
  to service_role;
commit;
