-- Wave 7.1: post_metrics — nightly Zernio analytics snapshots at post-age days
-- 1 | 3 | 7 | 28, deduped by platformPostId (primary key), external posts
-- flagged (calendar_id null, external=true — they inform the baseline but
-- never train the playbook). Exact CREATE TABLE from ECHO_A_GRADE_SPEC.md 7.1.
create table if not exists post_metrics (
  gym_id text not null, platform text not null, platform_post_id text not null,
  calendar_id uuid, external boolean not null default false,
  pillar text, format text, hook_family text, ask_type text, time_slot text,
  caption_len_band text, has_member_face boolean, media_product_type text,
  published_at timestamptz, snapshot_day int not null,   -- 1 | 3 | 7 | 28
  impressions int, reach int, likes int, comments int, shares int, saves int,
  clicks int, views int, follows int, watch_time_ms bigint, video_seconds int,
  followers_at_snapshot int,
  primary key (gym_id, platform, platform_post_id, snapshot_day)
);
