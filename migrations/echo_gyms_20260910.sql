-- echo_gyms: the SHARED, cross service record of Echo's per gym row.
--
-- WHY (the echo.db split brain, 2026-09-10). Echo runs as two Railway services out of
-- one repo, `echo` (the worker) and `echo-intake-web` (the HTTP service that serves
-- POST /portal/onboard). Each has its OWN Railway volume mounted at /data, and Railway
-- cannot mount one volume on two services, so each has its OWN /data/echo.db. Every
-- `gyms` row a self serve onboard wrote landed on the web service's volume, invisible
-- to the worker, which is where every read of that row actually happens. Measured
-- before this migration: 113 gym rows on echo-intake-web, 21 on echo.
--
-- Supabase is the one store both services already reach, so this table is the shared
-- record. agent/db.py dual writes to it on every gym_upsert and read through hydrates
-- the local SQLite row on a gym_get miss; agent/gym_store_sync.py is the one time heal.
--
-- KEYED BY account_key, exactly like the local table (NOT the portal gyms.id uuid):
-- Echo's own identifier for a gym is the account key, and every reader here already
-- holds one. echo_intake_tokens remains the account_key <-> gym_id bridge.
--
-- DELIBERATELY ABSENT: upload_link (it embeds the gym's RAW capability token),
-- intake_token_encrypted, intake_token_hash, token_sha256. None is read cross service
-- and every token is deterministically re mintable from AGENT_INTAKE_SIGNING_SECRET,
-- which both services already hold, so mirroring them would create a new place a live
-- token sits for zero benefit. See agent/gym_shared_store.MIRRORED_COLUMNS.
--
-- Additive only: creates one new table, touches nothing that exists.

create table if not exists public.echo_gyms (
  account_key              text primary key,
  display_name             text,
  gym_name                 text,
  token_revoked            integer,
  token_status             text,
  token_rotated_at         text,
  publish_flag             text,
  publish_creds_status     text,
  zernio_profile_id        text,
  zernio_default_fb_page_id text,
  posting_timezone         text,
  baseline_posts_per_week  double precision,
  baseline_captured_at     text,
  stripe_customer_id       text,
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now()
);

-- Same posture as the other Echo owned tables (gym_event, social_baseline): RLS ON
-- with NO policies, so service_role (which bypasses RLS) is the only reader/writer and
-- anon / authenticated see nothing. A gym's row is never client readable.
alter table public.echo_gyms enable row level security;

comment on table public.echo_gyms is
  'Shared cross service record of Echo per gym state. Written by both the echo worker and echo-intake-web via agent/db.gym_upsert. Never holds token material.';
