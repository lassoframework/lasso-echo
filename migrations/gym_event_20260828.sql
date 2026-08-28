-- Event Campaigns (EVENT_CAMPAIGNS_BUILD.md §3): the gym_event table.
--
-- An event IS an offer record: its start/end dates gate publishing exactly like
-- the offer rails. The window-timed campaign engine (the one that already runs
-- Summit) plans a dated ARC of content_calendar rows against one gym_event; LASSO
-- Summit itself becomes just another gym_event row on gym_id='lasso'.
--
-- ARMING STEP — apply by hand, in order, BEFORE arming AGENT_EVENT_CAMPAIGNS:
--   1. this file (gym_event table)
--   2. content_calendar_event_id_20260828.sql (content_calendar.event_id column)
-- Additive only: no existing table, column, or row is touched.

create table if not exists gym_event (
  id          text primary key,           -- app-generated stable id (gym|slug|starts)
  gym_id      text not null,              -- tenant scope; 'lasso' for the Summit row
  name        text not null,
  type        text not null,             -- bring_a_friend|challenge|open_house|
                                         -- anniversary|holiday_sale|new_offer|party
  starts_on   date not null,
  ends_on     date not null,
  tz          text not null,             -- the GYM'S IANA tz; dated posts fire here
  offer_text  text,                      -- what someone gets + what they do
  link        text,                      -- optional; verified before every publish
  brief       text,                      -- optional one-liner (grounds the copy)
  media_ids   jsonb default '[]'::jsonb, -- picked media pool asset ids (optional)
  status      text not null default 'draft'
                check (status in ('draft','scheduled','live','ended','cancelled')),
  created_by  text,                      -- owner|coach|lasso_coach actor id
  created_at  timestamptz default now(),
  audit       jsonb default '[]'::jsonb  -- append-only edit/cancel/on-behalf log
);

-- Tenant-scoped date lookups: the nightly status job and the overlap guard both
-- read a gym's events by (gym_id, window).
create index if not exists gym_event_gym_dates_idx
  on gym_event (gym_id, starts_on, ends_on);
create index if not exists gym_event_status_idx
  on gym_event (status);
