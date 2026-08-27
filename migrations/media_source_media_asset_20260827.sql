-- media_source + media_asset — the gym_media_drive index (gym_media_drive spec §2).
--
-- ARMING STEP: apply BY HAND to the Echo Supabase project (SQL editor or the
-- supabase migration tooling). This file is NOT applied automatically by any
-- code path; the sync job (agent/jobs/sync_gym_media.py) simply fails with a
-- clear store error until the tables exist.
--
-- ADDITIVE by design: this creates NEW tables next to the existing podcast_asset
-- table. It does NOT touch, migrate, or re-point podcast_asset. Re-pointing the
-- podcast library to run as a media_source row is a FLAGGED FOLLOW-UP (see the
-- definition-of-done), deliberately NOT done in this pass to avoid conflicting
-- with the in-flight podcast pipeline branch.

-- ---- media_source: one connected Drive folder per gym ------------------------
-- One row = one Drive folder a gym connected. kind lets the SAME table later host
-- the podcast library as just another source (the follow-up). folder_id is UNIQUE
-- GLOBALLY (across every gym) — this is the hijack fix (spec §1.5a): a folder
-- already bound anywhere is hard-refused at bind time, so gym B can never point at
-- gym A's already-connected folder.
CREATE TABLE IF NOT EXISTS media_source (
  id            text PRIMARY KEY,          -- uuid/opaque; NOT the folder id
  gym_id        text NOT NULL,             -- the tenant base key (e.g. 'pierce')
  kind          text NOT NULL DEFAULT 'gym_drive'
                CHECK (kind IN ('gym_drive', 'podcast_library')),
  folder_id     text NOT NULL,             -- the Drive folder id (parsed at bind)
  folder_name   text,                      -- confirmed name at bind (audit/UI)
  owner_email   text,                      -- folder owner at bind (ownership rail)
  sync_mode     text NOT NULL DEFAULT 'all'
                CHECK (sync_mode IN ('all', 'selected')),
  active        boolean NOT NULL DEFAULT true,
  revoked_externally boolean NOT NULL DEFAULT false,  -- a later sync 403'd on it
  connected_by  text,                      -- actor id who bound it
  connected_at  timestamptz NOT NULL,
  CONSTRAINT media_source_folder_unique UNIQUE (folder_id)   -- GLOBAL: the hijack fix
);

CREATE INDEX IF NOT EXISTS media_source_gym_idx
  ON media_source (gym_id, active);

-- ---- media_asset: one Drive file, indexed + gated ---------------------------
-- id is the Drive file id (stable across re-index). gym_id is denormalized onto
-- every asset so tenant isolation is enforceable at read AND stage time (spec
-- §1.5d: pick_media filters by gym_id AND the publish path asserts
-- asset.gym_id == row.gym_id). content_hash (Drive md5Checksum) is the dedupe key.
-- eligible is the photo/video gate result; excluded_by_coach is the portal hide.
CREATE TABLE IF NOT EXISTS media_asset (
  id                text PRIMARY KEY,      -- drive file id
  source_id         text NOT NULL REFERENCES media_source (id) ON DELETE CASCADE,
  gym_id            text NOT NULL,         -- denormalized for tenant isolation
  kind              text NOT NULL          -- 'photo' | 'video' | 'other'
                    CHECK (kind IN ('photo', 'video', 'other')),
  title             text NOT NULL,
  mime_type         text,
  size_bytes        bigint,
  content_hash      text,                  -- Drive md5Checksum; dedupe key
  duration_sec      numeric,               -- videos: probed on first download
  width             int,
  height            int,
  aspect            text,                  -- '4:5'..'1.91:1' band label, or 'other'
  crop_hint         text,                  -- nearest legal crop when out of band
  vision_json       jsonb,                 -- ECHO_VISION analysis (photos), null until run
  rendition_key     text,                  -- Echo-bucket key of a HEIC/HEVC conversion
  rendition_url     text,                  -- public url of the rendition (cache)
  eligible          boolean,               -- gate result; null until decidable
  excluded_by_coach boolean NOT NULL DEFAULT false,
  reject_reason     text,
  used_count        int  NOT NULL DEFAULT 0,
  last_used_at      timestamptz,
  drive_modified    timestamptz,           -- Drive modifiedTime (change detection)
  indexed_at        timestamptz NOT NULL
);

-- The selector's hot path: candidates for a gym, ordered least-used/longest-unused.
CREATE INDEX IF NOT EXISTS media_asset_selector_idx
  ON media_asset (gym_id, eligible, excluded_by_coach, used_count, last_used_at);

-- Dedupe lookups + source-scoped sweeps.
CREATE INDEX IF NOT EXISTS media_asset_hash_idx  ON media_asset (gym_id, content_hash);
CREATE INDEX IF NOT EXISTS media_asset_source_idx ON media_asset (source_id);
