-- DRAFT MIGRATION — review before applying (Blake ruled: migration comes as a draft first)
-- Portal Supabase project: lasso-ops-portal (ooqcvmcjspeltuuhcvlh)
--
-- Purpose (Dale round-2 §5c): a STORY publishes with an empty body, so its caption lives
-- ONLY on the burned media (image_url). When a client edits a story caption, the raw
-- source photo/video is not stored on the row, so Echo cannot re-burn the caption
-- immediately — today the edit only lands on the NEXT monthly rebuild. This column stores
-- the story's RAW source media url at plan time so an edited story caption re-renders
-- right away.
--
-- Safe/additive: nullable text, no default, no backfill. Nothing reads or writes it until
-- AGENT_STORY_SOURCE_MEDIA is armed on the worker (default OFF), so applying this migration
-- alone changes NO behavior. Sequence: (1) apply this, (2) set AGENT_STORY_SOURCE_MEDIA=true
-- on the echo + echo-intake-web services.

ALTER TABLE content_calendar
  ADD COLUMN IF NOT EXISTS source_media_url text;

COMMENT ON COLUMN content_calendar.source_media_url IS
  'Story rows only: the raw (un-burned) source photo/video url captured at plan time, so an '
  'edited story caption can be re-burned onto fresh media immediately instead of waiting for '
  'the monthly rebuild. Written/read only when AGENT_STORY_SOURCE_MEDIA is on. Null elsewhere.';
