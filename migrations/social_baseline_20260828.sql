-- Migration: social_baseline_20260828
-- BEFORE/AFTER social metrics (agent/social_baseline.py, flag AGENT_SOCIAL_BASELINE).
-- One IMMUTABLE row per gym: the rubric measures over the 90 days of PUBLIC
-- Instagram feed ending at the gym's Echo start, captured once via Apify.
-- The PRIMARY KEY (gym_id) is the immutability rail: the writer is a plain
-- INSERT (no upsert), so a second capture conflicts and is refused in code.
-- Additive and idempotent: CREATE TABLE IF NOT EXISTS only; re-running is safe.

CREATE TABLE IF NOT EXISTS social_baseline (
    gym_id       text        NOT NULL,
    ig_handle    text        NOT NULL,
    echo_start   date        NOT NULL,
    window_start date        NOT NULL,
    window_end   date        NOT NULL,
    measures     jsonb       NOT NULL DEFAULT '{}',
    captured_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (gym_id)
);
