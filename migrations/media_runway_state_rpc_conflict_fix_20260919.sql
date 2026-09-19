-- Repair the initial shared-runway CAS function after production exposed a
-- PL/pgSQL name collision between its RETURNS TABLE gym_id output variable and
-- the unqualified conflict-column insert target.

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
