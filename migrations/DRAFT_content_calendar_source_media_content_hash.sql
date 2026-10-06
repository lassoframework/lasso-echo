-- DRAFT MIGRATION — review before applying (Blake ruled: migration comes as a draft
-- first). Portal Supabase project: lasso-ops-portal (ooqcvmcjspeltuuhcvlh).
-- Keep DRAFT/unapplied until cloud Ultra Review and the normal release gates pass.
--
-- Purpose (echo-drive-source-lineage handoff, 2026-10-05): gym_media_drive rows
-- staged from a gym's connected Drive folder already carry source_media_asset_id
-- (the Drive file id). What they did NOT carry is the original-byte identity when
-- the delivered image_url is a TRANSFORMED rendition (HEIC->JPEG / HEVC->H.264):
-- a rendition URL is a delivery address, not source evidence. media_asset
-- indexes the Drive md5Checksum at sync time (media_asset.content_hash), which is
-- trustworthy original-source identity. This column carries that hash onto the
-- staged calendar row for BOTH original-served and rendition-backed drafts, so a
-- foreign-gym, unmatched, or unknown asset can never be cleared by guessing from
-- its delivery URL.
--
-- Safe/additive: nullable text, no default, no backfill. Historically staged rows
-- stay NULL (no bulk backfill is authorized); the nightly pipeline re-stamps new
-- rows as assets flow. Nothing reads or writes it until the builder/mirror path
-- that stamps it is released; pre-migration inserts omit the key entirely.
ALTER TABLE content_calendar
  ADD COLUMN IF NOT EXISTS source_media_content_hash text;

COMMENT ON COLUMN content_calendar.source_media_content_hash IS
  'gym_media_drive rows: the Drive md5Checksum (media_asset.content_hash) of the '
  'original source bytes the draft was staged from. Set for original-served and '
  'rendition-backed drafts alike; distinct from image_url / source_media_url, '
  'which are delivery addresses. NULL for historically staged rows (no backfill).';
