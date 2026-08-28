-- content_calendar.source_media_asset_id + media_not_ready_reason
-- (gym_media_drive spec §4, §8 — the media-not-ready flip).
--
-- ARMING STEP: apply BY HAND to the Echo content_calendar Supabase project (SQL
-- editor or the supabase migration tooling) BEFORE flipping GYM_DRIVE_STAGE on.
-- This file is NOT applied automatically by any code path.
--
-- WHY: when the gym-media builder stages a Drive photo into a PENDING calendar
-- row, it stamps source_media_asset_id with the media_asset id it consumed. That
-- lets two flip paths reset the row back to needs_media the moment the media is no
-- longer usable:
--   * the portal HIDE action (agent/gym_media_routes.py:_flip_pending_using_asset)
--   * the nightly removed-from-Drive sweep (agent/jobs/sync_gym_media.py:
--     _flip_pending_for_missing)
-- Both PATCH content_calendar filtering source_media_asset_id=eq.<id> and set
-- media_not_ready_reason. Without these columns those PATCHes 400 and the flip is a
-- silent no-op (the live GET returns 400 today), so a hidden/removed photo would
-- keep a stale PENDING post pointing at media that no longer exists.
--
-- Safe/additive: two nullable text columns, no default, no backfill. Nothing reads
-- or writes source_media_asset_id until GYM_DRIVE_STAGE is armed on the worker
-- (default OFF); nothing reads media_not_ready_reason until a flip fires. Applying
-- this migration alone changes NO behavior. Sequence: (1) apply this, (2) apply
-- migrations/media_source_media_asset_20260827.sql if not yet applied,
-- (3) set GYM_DRIVE_STAGE=true on the echo + echo-intake-web services.

ALTER TABLE content_calendar
  ADD COLUMN IF NOT EXISTS source_media_asset_id text;

ALTER TABLE content_calendar
  ADD COLUMN IF NOT EXISTS media_not_ready_reason text;

-- The flip paths filter pending rows by (gym_id, status, source_media_asset_id);
-- a partial index keeps that PATCH cheap without touching the hot planner path.
CREATE INDEX IF NOT EXISTS content_calendar_source_media_asset_idx
  ON content_calendar (gym_id, source_media_asset_id)
  WHERE source_media_asset_id IS NOT NULL;

COMMENT ON COLUMN content_calendar.source_media_asset_id IS
  'gym_media_drive rows only: the media_asset id (Drive file id) this PENDING post '
  'was staged from, so hiding or removing that asset flips the row back to '
  'needs_media. Written only when GYM_DRIVE_STAGE is on. Null for every other row.';

COMMENT ON COLUMN content_calendar.media_not_ready_reason IS
  'Why a row was flipped back to needs_media: media_hidden (coach hid the asset) or '
  'removed_from_drive (the asset vanished from the connected folder). Null otherwise.';
