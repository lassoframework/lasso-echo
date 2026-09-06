-- Migration: post_metrics_captured_at_20260905
-- Additive and idempotent: ADD COLUMN IF NOT EXISTS only. Safe to re-run.
--
-- AUD-006 was reported as "the post_metrics pull is not running daily", on the evidence
-- that the newest published_at is 2026-09-03. That evidence does not support that
-- conclusion, and the reason it does not is the whole point of this column.
--
-- published_at is when the POST was published. It is a fact about the gym's content, not
-- about Echo's pull. "The newest post we hold metrics for was published on the 3rd" is
-- exactly what a healthy system looks like when day 1 snapshots for the 3rd landed on
-- the 4th and the 4th's posts are not due until the 5th. There is NO column anywhere in
-- post_metrics recording when a snapshot was actually taken, so the question "did the
-- nightly run" is not answerable from this table at all, in either direction. That is
-- the real defect: not that the pull is broken, but that nobody can tell.
--
-- captured_at closes it. It is nullable and NOT backfilled: rows written before this
-- migration have no honest value, and stamping them with now() or with their
-- published_at would be inventing a measurement time. NULL here means "written before
-- Echo recorded when it pulled", which is the truth.

alter table post_metrics add column if not exists captured_at timestamptz;

comment on column post_metrics.captured_at is
    'When Echo took this snapshot. NOT the post publish time (that is published_at). '
    'NULL on rows written before migration post_metrics_captured_at_20260905; never '
    'backfilled, because a guessed capture time is a fabricated measurement.';
