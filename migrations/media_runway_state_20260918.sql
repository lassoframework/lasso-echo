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
  revision bigint not null,
  -- SQL NULL is the explicit "episode cleared after replenishment" state.
  -- It is not used for unavailable reads, which never fabricate a row.
  fallback_episode jsonb check (
    fallback_episode is null or jsonb_typeof(fallback_episode) = 'object'
  ),
  notice_state jsonb not null check (jsonb_typeof(notice_state) = 'object'),
  updated_at timestamptz not null default now()
);

-- Keep this migration idempotent if a pre-review copy of the table exists.
alter table public.media_runway_state
  add column if not exists revision bigint;
update public.media_runway_state set revision = 1 where revision is null;
alter table public.media_runway_state alter column revision set not null;

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'media_runway_state_revision_positive'
      and conrelid = 'public.media_runway_state'::regclass
  ) then
    alter table public.media_runway_state
      add constraint media_runway_state_revision_positive check (revision > 0);
  end if;
end
$$;

alter table public.media_runway_state enable row level security;

-- Browser roles have no table access and there are no policies.  Only Echo's
-- server-side service role may read or write this internal cross-service state.
revoke all on table public.media_runway_state from public, anon, authenticated;
revoke insert, update, delete on table public.media_runway_state from service_role;
grant select on table public.media_runway_state to service_role;

-- Remove the pre-review four-argument function.  PostgreSQL overloads by
-- signature, so leaving it behind would preserve an unguarded write path after
-- the CAS-aware function below is installed on an existing database.
drop function if exists public.upsert_media_runway_state(text, bigint, jsonb, jsonb);

create or replace function public.upsert_media_runway_state(
  p_gym_id text,
  p_expected_revision bigint,
  p_revision bigint,
  p_fallback_episode jsonb,
  p_notice_state jsonb
)
returns table (
  gym_id text,
  revision bigint,
  fallback_episode jsonb,
  notice_state jsonb,
  updated_at timestamptz
)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if nullif(btrim(p_gym_id), '') is null
      or p_gym_id <> btrim(p_gym_id)
      or p_expected_revision is null or p_expected_revision < 0
      or p_revision is null or p_revision <= p_expected_revision then
    raise exception 'invalid media runway tenant or revision';
  end if;

  if p_expected_revision = 0 then
    insert into public.media_runway_state (
      gym_id, revision, fallback_episode, notice_state, updated_at
    ) values (
      p_gym_id, p_revision, p_fallback_episode, p_notice_state, now()
    )
    -- The RETURNS TABLE output variable is also named gym_id.  Target the
    -- primary-key constraint explicitly so PL/pgSQL never treats gym_id as an
    -- ambiguous output variable during the insert path.
    on conflict on constraint media_runway_state_pkey do nothing;
  else
    update public.media_runway_state as current_state
       set revision = p_revision,
           fallback_episode = p_fallback_episode,
           notice_state = p_notice_state,
           updated_at = now()
     where current_state.gym_id = p_gym_id
       and current_state.revision = p_expected_revision;
  end if;

  -- Always return the current row.  The caller accepts the CAS only when this
  -- row exactly matches its proposed revision and snapshot; a concurrent winner
  -- therefore becomes a clean false result followed by a fresh read/retry.
  return query
    select state.gym_id, state.revision, state.fallback_episode,
           state.notice_state, state.updated_at
    from public.media_runway_state as state
    where state.gym_id = p_gym_id;
end
$$;

revoke all on function public.upsert_media_runway_state(text, bigint, bigint, jsonb, jsonb)
  from public, anon, authenticated;
grant execute on function public.upsert_media_runway_state(text, bigint, bigint, jsonb, jsonb)
  to service_role;

comment on table public.media_runway_state is
  'Server-only tenant-scoped media runway projection for Echo worker and portal services; contains no token, credential, Slack route, notice text, or client-write state.';
