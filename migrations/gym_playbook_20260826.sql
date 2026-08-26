-- Wave 7.5 / 7.8: gym_playbook (versioned, insert-only — old versions are
-- never mutated) and monthly_retro (one row per gym per closed month).
-- Exact CREATE TABLE statements from ECHO_A_GRADE_SPEC.md 7.8.
create table if not exists gym_playbook (
  gym_id text not null, version int not null, updated_by text not null,
  playbook jsonb not null, evidence jsonb, created_at timestamptz default now(),
  primary key (gym_id, version)
);
create table if not exists monthly_retro (
  gym_id text not null, month date not null, findings jsonb not null,
  playbook_diff jsonb, tainted boolean not null default false,
  created_at timestamptz default now(), primary key (gym_id, month)
);
