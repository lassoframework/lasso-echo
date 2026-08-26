-- Hook-quality metric fields (rides AGENT_METRICS_SYNC): reels skip rate,
-- total watch time, platform engagement rate, and the is_ad flag. is_ad rows
-- are observed only and NEVER train the playbook (same treatment as external).
-- Applied to Supabase project ooqcvmcjspeltuuhcvlh on 2026-08-26 as
-- migration "post_metrics_hook_fields_20260827".
alter table post_metrics add column if not exists reels_skip_rate real;
alter table post_metrics add column if not exists watch_total_ms bigint;
alter table post_metrics add column if not exists engagement_rate real;
alter table post_metrics add column if not exists is_ad boolean not null default false;
