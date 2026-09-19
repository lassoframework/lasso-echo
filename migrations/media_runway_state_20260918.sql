-- Shared read model for the worker and echo-intake-web media runway projection.
--
-- gym_id is Echo's canonical tenant base/account key (for example ``gymx``),
-- not a portal UUID.  Every application read and upsert carries this key, so
-- neither service has to resolve an unscoped UUID before accessing the row.
-- The json columns hold only the explicit portal-safe projections validated by
-- agent/shared_media_runway_store.py: no Slack route/text/timestamp, upload URL,
-- token, credential, or client-supplied write belongs here.

create table if not exists public.media_runway_state (
  gym_id text primary key check (nullif(btrim(gym_id), '') is not null),
  -- SQL NULL is the explicit "episode cleared after replenishment" state.
  -- It is not used for unavailable reads, which never fabricate a row.
  fallback_episode jsonb check (
    fallback_episode is null or jsonb_typeof(fallback_episode) = 'object'
  ),
  notice_state jsonb not null check (jsonb_typeof(notice_state) = 'object'),
  updated_at timestamptz not null default now()
);

alter table public.media_runway_state enable row level security;

-- Browser roles have no table access and there are no policies.  Only Echo's
-- server-side service role may read or write this internal cross-service state.
revoke all on table public.media_runway_state from public, anon, authenticated;
grant select, insert, update on table public.media_runway_state to service_role;

comment on table public.media_runway_state is
  'Server-only tenant-scoped media runway projection for Echo worker and portal services; contains no token, credential, Slack route, notice text, or client-write state.';
