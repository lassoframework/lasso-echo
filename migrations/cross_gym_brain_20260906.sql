-- Migration: cross_gym_brain_20260906
-- The FLEET rollup written nightly by agent/jobs/cross_gym_brain.py
-- (flag AGENT_CROSS_GYM_BRAIN, default OFF). One row per run.
--
-- APPEND ONLY. There is no natural key and no upsert anywhere in the writer:
-- insert_rollup() is a plain POST. The history of what the fleet believed, and
-- when, is the point. Nothing in Echo updates or deletes a row here.
--
-- FORM ONLY. findings and guidance carry lever NAMES, lever VALUE tokens, and
-- numbers, and nothing else. The writer runs every artifact through
-- cross_gym_brain.form_only_violations() (a whitelist of allowed lever names and
-- lever values) and REFUSES to write when any string in it is not a known token
-- or an ISO timestamp. No caption text, stat, offer, member name, handle, or
-- gym_id is ever stored in this table.
--
-- One findings entry looks like:
--   {"lever": "hook_family", "format_stratum": "feed", "value": "question",
--    "n": 11, "gyms": 3, "n_other": 24, "gyms_other": 4,
--    "mean_log_engagement": 0.1841, "mean_log_engagement_other": 0.1102,
--    "effect_size": 0.41, "p_value": 0.031, "q_value": 0.093,
--    "verdict": "directional"}
-- Only a "supported" finding (both cells at or above the sample floor, both
-- drawn from >= 2 distinct gyms, surviving Benjamini Hochberg at alpha, and with
-- |Cohen's d| >= the effect floor) produces a guidance entry.
--
-- Additive and idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS only.

CREATE TABLE IF NOT EXISTS cross_gym_brain (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_at            timestamptz NOT NULL DEFAULT now(),
    window_start      date        NOT NULL,
    window_end        date        NOT NULL,
    window_days       int         NOT NULL,
    gyms_contributing int         NOT NULL DEFAULT 0,
    posts_analyzed    int         NOT NULL DEFAULT 0,
    findings          jsonb       NOT NULL DEFAULT '[]'::jsonb,
    guidance          jsonb       NOT NULL DEFAULT '[]'::jsonb,
    context           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cross_gym_brain_run_at_idx
    ON cross_gym_brain (run_at DESC);
