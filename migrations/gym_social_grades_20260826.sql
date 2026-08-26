-- Migration: gym_social_grades_20260826
-- Stores per-gym calendar grades produced by agent/jobs/grade_sweep.py.
-- Applied to Supabase project ooqcvmcjspeltuuhcvlh on 2026-08-26 (success:true).
-- Re-running this migration is safe: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS gym_social_grades (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    gym_id      text        NOT NULL,
    window      text        NOT NULL CHECK (window IN ('trailing_30', 'forward_book')),
    total       integer     NOT NULL,
    letter      text        NOT NULL,
    scores      jsonb       NOT NULL DEFAULT '{}',
    defects     jsonb       NOT NULL DEFAULT '[]',
    graded_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS gym_social_grades_gym_window
    ON gym_social_grades (gym_id, window, graded_at DESC);
