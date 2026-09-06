-- Migration: social_metrics_daily_provenance_20260905
-- Track 4 (zernio/apify). Additive and idempotent: ADD COLUMN IF NOT EXISTS and
-- CREATE INDEX IF NOT EXISTS only. Drops nothing, rewrites no rows, safe to re-run.
--
-- Three things:
--
-- 1. gym_social_metrics_daily gains `platform` and `source`, so a reader can tell which
--    lane a row describes and which system produced it WITHOUT opening the jsonb. Both
--    are nullable on purpose: rows written before this migration have no honest value and
--    a backfilled guess would be fabrication. NULL here means "written before provenance
--    was recorded", which is the truth. (The table is empty today, so in practice every
--    row will carry both, but the column stays nullable so that stays a fact about the
--    data rather than a constraint that would have to be satisfied by inventing values.)
--
-- 2. NO new unique index. gym_social_metrics_daily ALREADY has a unique constraint on
--    (late_account_id, metric_date), verified live 2026-09-05 with a non mutating probe:
--    an upsert whose on_conflict target was (late_account_id, metric_date) got 23503
--    (only the deliberate foreign key violation stopped it, so the target resolved),
--    while (gym_id, late_account_id, metric_date) got 42P10 "there is no unique or
--    exclusion constraint matching the ON CONFLICT specification". An earlier draft of
--    this migration would have added that second index and the writer would have
--    targeted it, which is a 400 on every single write in production. Probing the live
--    constraint rather than trusting the draft is what caught it. The writer now targets
--    (late_account_id, metric_date), which is sufficient: a Zernio account id belongs to
--    exactly one gym, so it already determines gym_id.
--
-- 3. echo_social_connections gains `late_account_id`. This is the AUD-005 fix. Echo has
--    two disagreeing pictures of who is connected: echo_social_connections (written by
--    the 6h reverify sweep, verified live 2026-09-05 12:01, 44 connected rows across 19
--    gyms, matching Zernio's own health read of 44 social accounts EXACTLY) and
--    gym_social_accounts (42 rows across 15 gyms, status always 'active' with no
--    expired state at all, missing CrossFit Chateau and MFLH entirely, spelling the
--    Google lane 'google_business' where every other system spells it 'googlebusiness',
--    and written by NOTHING in this repo). echo_social_connections is the source of
--    truth. The only thing gym_social_accounts still carried that the truth table did
--    not was the Zernio account id, so it moves here and the legacy table has no
--    remaining reason to be read.

alter table gym_social_metrics_daily add column if not exists platform text;
alter table gym_social_metrics_daily add column if not exists source text;

-- coalesce() rather than the bare column: in Postgres two NULLs are DISTINCT in a
-- unique index, so a bare (gym_id, late_account_id, metric_date) would let unlimited
-- duplicate rows through for any account id that came back null. The writer always
-- supplies late_account_id, and this index makes that a guarantee rather than a habit.
create unique index if not exists gym_social_metrics_daily_gym_account_date_key
    on gym_social_metrics_daily (gym_id, coalesce(late_account_id, ''), metric_date);

alter table echo_social_connections add column if not exists late_account_id text;
