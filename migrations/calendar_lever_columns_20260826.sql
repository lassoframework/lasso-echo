-- Wave 7.2: additive lever columns on content_calendar, stamped at draft time
-- (behind AGENT_LEARNING_LOOP) so the monthly retro can learn levers it can
-- see. Additive only: no existing column or row is touched.
alter table content_calendar add column if not exists hook_family text;
alter table content_calendar add column if not exists ask_type text;
alter table content_calendar add column if not exists time_slot text;
alter table content_calendar add column if not exists caption_len_band text;
alter table content_calendar add column if not exists has_member_face boolean;
