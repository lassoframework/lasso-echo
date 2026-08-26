-- Wave 7.7: labeled experiments — ~15% of slots per month carry an
-- experiment_label ('<lever>:<YYYY-MM>'), one lever under test per gym per
-- month. Additive only.
alter table content_calendar add column if not exists experiment_label text;
