-- podcast_asset — the Echo podcast library index (PODCAST_LIBRARY_BUILD_SPEC.md §2.1).
--
-- ARMING STEP: apply BY HAND to the Echo Supabase project (SQL editor or
-- supabase migration tooling). This file is NOT applied automatically by any
-- code path; the indexer (agent/jobs/index_podcast_library.py) simply fails
-- with a clear store error until the table exists.
--
-- Columns are the spec schema verbatim. id is the Drive file id. postable is
-- TRI-STATE: true (passed the gate), false (rejected, see reject_reason), or
-- NULL (not yet ffprobed — never selectable; fail closed).
-- used_count / last_used_at belong to the SELECTOR (stamped at stage time,
-- rolled back on coach deny); the indexer never writes them.

CREATE TABLE IF NOT EXISTS podcast_asset (
  id              text PRIMARY KEY,      -- drive file id
  episode         int  NOT NULL,         -- from parent folder title
  kind            text NOT NULL,         -- 'clip' | 'audiogram' | 'full_video' | 'audio' | 'notes'
  clip_index      int,                   -- 1..4 for clips, null otherwise
  title           text NOT NULL,
  size_bytes      bigint,
  duration_sec    numeric,               -- probed on first download, null until then
  width           int,
  height          int,
  aspect          text,                  -- '9:16' | '1:1' | '16:9' | 'other'
  postable        boolean,               -- computed, spec §2.3; null until probed
  reject_reason   text,
  used_count      int  NOT NULL DEFAULT 0,
  last_used_at    timestamptz,
  notes_doc_id    text,                  -- the episode's Google Doc
  indexed_at      timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS podcast_asset_selector_idx
  ON podcast_asset (postable, used_count, last_used_at);
