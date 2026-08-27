-- CADENCE_SPEC.md (Blake 2026-08-27): per-gym posting cadence (1x/2x per day).
-- Additive only; both columns nullable-safe with today's behavior as the default.
--
-- 1. echo_gym_settings.posts_per_day: the gym's stored cadence preference,
--    written by the portal toggle (set_gym_posts_per_day). Default 1 = today.
-- 2. echo_gym_settings.cadence_updated_by: audit trail, mirrors autonomy_updated_by.
-- 3. content_calendar.slot_index: the 2x plan's slot ordinal (0=AM, 1=PM) on rows
--    built by a 2x plan; NULL on every 1x row (publish-time hashing unchanged).
alter table public.echo_gym_settings
  add column if not exists posts_per_day int not null default 1
    check (posts_per_day in (1, 2));
alter table public.echo_gym_settings
  add column if not exists cadence_updated_by text;
alter table public.content_calendar
  add column if not exists slot_index int
    check (slot_index is null or slot_index in (0, 1));
