-- Per-gym engaged-audience demographics (flag AGENT_AUDIENCE_DEMOGRAPHICS,
-- default OFF): weekly snapshots of Zernio's Instagram demographics, both
-- follower and engaged-audience breakdowns, stored as-is (never guessed).
-- Applied to Supabase project ooqcvmcjspeltuuhcvlh on 2026-08-26 as
-- migration "gym_audience_demographics_20260827".
create table if not exists gym_audience_demographics (
  gym_id text not null,
  captured_at date not null,
  kind text not null check (kind in ('followers','engaged')),
  breakdown jsonb,
  primary key (gym_id, captured_at, kind)
);
