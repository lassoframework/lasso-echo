-- Story Studio — story_request + story_render + render_ledger + story_sort_queue
-- (ECHO_STORY_STUDIO_BUILD §0, §4).
--
-- ARMING STEP: apply BY HAND to the Echo Supabase project (SQL editor or the
-- supabase migration tooling). This file is NOT applied automatically by any
-- code path. Until the tables exist:
--   * story_studio_store raises a clear StoryStudioStoreError (nothing crashes);
--   * story_ledger and story_sort_queue fall back to the volume kv store, so the
--     re-ingest guard and the sort queue keep working on a single box / in tests.
--
-- ADDITIVE by design: these are NEW tables next to media_source / media_asset.
-- They do NOT touch, migrate, or re-point any existing table.
--
-- TENANT ISOLATION: story_request + story_render + story_sort_queue all carry
-- gym_id; every read REQUIRES a gym_id and filters on it (the same three-gate
-- model as media_source_store). The composer additionally asserts asset.gym_id ==
-- request.gym_id on EVERY segment before a plan is built.

-- ---- story_request: one Create-a-Story request from the portal lane ----------
-- One row = one coach's "Create a Story" tap. asset_ids is the footage the coach
-- picked from the gym's raw pool; brief is the optional "What's this about?" line
-- that grounds the overlay + caption (never contradicted, never added to).
CREATE TABLE IF NOT EXISTS story_request (
  id            text PRIMARY KEY,          -- uuid/opaque request id
  gym_id        text NOT NULL,             -- the tenant base key (e.g. 'pierce')
  asset_ids     jsonb NOT NULL DEFAULT '[]'::jsonb,  -- picked raw pool asset ids
  brief         text,                      -- optional "What's this about?" one-liner
  template      text,                      -- resolved template name (declared|vision)
  music_mood    text,                      -- selected shelf: 'hype'|'chill'|'none'
  requested_by  text,                      -- actor id who requested it
  status        text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'held', 'staged', 'denied')),
  hold_reason   text,                      -- honest reason when a request is HELD
  deny_reason   text,                      -- coach's reason when a staged story is denied
  created_at    timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS story_request_gym_idx
  ON story_request (gym_id, status, created_at);

-- ---- story_render: the rendered output of a request --------------------------
-- One row = one 1080x1920 render. segment_plan is the ordered slice plan; every
-- render carries the licensed music honesty fields (track_id + license_ref), the
-- content_hash (also written to render_ledger, the re-ingest guard), and the
-- calendar_row_id of the PENDING draft it staged. A render is ALWAYS status
-- 'pending' when written; the human approval tap is untouched.
CREATE TABLE IF NOT EXISTS story_render (
  id                 text PRIMARY KEY,     -- render id (== request id in this build)
  request_id         text NOT NULL REFERENCES story_request (id) ON DELETE CASCADE,
  gym_id             text NOT NULL,        -- denormalized for tenant isolation
  segment_plan       jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{asset_id,start_ts,end_ts,score}]
  overlay_text_final text,                 -- the final, gated, framed overlay copy
  overlay_flags      jsonb NOT NULL DEFAULT '[]'::jsonb,  -- e.g. 'no brief, edit before approving'
  grounded_from      text,                 -- 'brief' | 'vision' | 'generic_safe'
  template           text,
  track_id           text,                 -- licensed bed id (empty for shelf 'none')
  license_ref        text,                 -- LASSO library license ref (empty for 'none')
  music_shelf        text,                 -- 'hype' | 'chill' | 'none'
  content_hash       text,                 -- sha256 of the render bytes; the ledger key
  calendar_row_id    text,                 -- the PENDING draft id this render staged
  status             text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'denied')),
  created_at         timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS story_render_gym_idx     ON story_render (gym_id, status, created_at);
CREATE INDEX IF NOT EXISTS story_render_request_idx ON story_render (request_id);
CREATE INDEX IF NOT EXISTS story_render_hash_idx    ON story_render (content_hash);

-- ---- render_ledger: the RE-INGEST GUARD (§0, the EP124 lesson) ---------------
-- Every Story render's content_hash lands here. When a render is saved back into
-- a client's Drive and later walked by the media sync, its content_hash is
-- recognized HERE and the file is skipped (never re-indexed as raw, never
-- re-composed, never reposted). content_hash is the PRIMARY KEY so a write is
-- idempotent (the same bytes record once). Cross-service (the sync job and the
-- render lane run on different boxes) so it lives in Supabase, with a kv mirror.
CREATE TABLE IF NOT EXISTS render_ledger (
  content_hash    text PRIMARY KEY,        -- sha256 of an Echo render's bytes (normalized)
  gym_id          text,                    -- the gym the render was made for
  story_render_id text,                    -- the story_render.id it came from
  recorded_at     timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS render_ledger_gym_idx ON render_ledger (gym_id);

-- ---- story_sort_queue: the "Sort these" ambiguous queue (§0.3) ---------------
-- When the classifier cannot confidently call a file raw or finished, it is
-- NEVER auto-staged: it lands here for a human (tap Raw / Finished / Skip in the
-- portal). enqueue is idempotent per (gym_id, asset_id). A coach-channel digest
-- fires only when the queue is non-empty. A silent wrong guess is the only
-- unacceptable outcome, so this queue is the safety net under the classifier.
CREATE TABLE IF NOT EXISTS story_sort_queue (
  gym_id        text NOT NULL,
  asset_id      text NOT NULL,
  thumbnail_url text,
  reasons       jsonb NOT NULL DEFAULT '[]'::jsonb,  -- the classifier's signal notes
  verdict       text NOT NULL DEFAULT 'ambiguous',
  status        text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'resolved')),
  enqueued_at   timestamptz NOT NULL,
  resolved_lane text,                      -- 'raw' | 'finished' | 'skip' (on resolve)
  resolved_by   text,
  resolved_at   timestamptz,
  PRIMARY KEY (gym_id, asset_id)           -- idempotent enqueue per gym+asset
);

CREATE INDEX IF NOT EXISTS story_sort_queue_pending_idx
  ON story_sort_queue (gym_id, status, enqueued_at);
