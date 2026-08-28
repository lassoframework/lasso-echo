-- Event Campaigns (EVENT_CAMPAIGNS_BUILD.md §3): tag every arc post with its
-- gym_event. An arc row carries event_id = gym_event.id so the status job, the
-- cancel/ended sweep, the dead-link guard, and the overlap cap can find exactly
-- the rows a given event owns (and only those). Additive only.
--
-- ARMING STEP — apply AFTER gym_event_20260828.sql, BEFORE arming
-- AGENT_EVENT_CAMPAIGNS.
alter table content_calendar add column if not exists event_id text;

-- Fast "all rows for this event" reads (cancel/ended sweep, dead-link revert,
-- overlap cap). Partial index: only arc rows carry an event_id.
create index if not exists content_calendar_event_id_idx
  on content_calendar (event_id) where event_id is not null;
