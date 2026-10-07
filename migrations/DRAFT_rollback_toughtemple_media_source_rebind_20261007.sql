-- DRAFT / UNAPPLIED rollback for
-- DRAFT_toughtemple_media_source_rebind_20261007.sql.
--
-- This restores the former stale source key and therefore restores the known
-- fail-closed state. Use only if the forward rebind has to be undone before any
-- sync/media write occurred. It does not restore service. If the audited asset
-- manifest changed, stop; do not force this rollback.

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
LOCK TABLE public.gyms, public.echo_intake_tokens,
           public.media_source, public.media_asset,
           public.content_calendar
  IN SHARE ROW EXCLUSIVE MODE;

DO $rollback$
DECLARE
  v_source public.media_source%ROWTYPE;
  v_count integer;
  v_assets integer;
  v_photos integer;
  v_videos integer;
  v_bad_assets integer;
  v_bad_calendar integer;
  v_source_triggers integer;
  v_changed integer;
BEGIN
  SELECT * INTO v_source
    FROM public.media_source
   WHERE id = '389f7e3157514588b8598784b60f9123'
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Tough Temple media rollback refused: expected source row is missing';
  END IF;
  IF v_source.gym_id NOT IN ('toughtemple52040e', 'toughtemple086f51')
     OR v_source.kind IS DISTINCT FROM 'gym_drive'
     OR v_source.active IS DISTINCT FROM true
     OR v_source.revoked_externally IS DISTINCT FROM false
     OR v_source.sync_status IS DISTINCT FROM 'idle'
     OR v_source.sync_requested_at IS NOT NULL
     OR v_source.sync_claim_token IS NOT NULL THEN
    RAISE EXCEPTION 'Tough Temple media rollback refused: source state changed or work is queued';
  END IF;
  SELECT count(*) INTO v_source_triggers
    FROM pg_trigger
   WHERE tgrelid = 'public.media_source'::regclass
     AND NOT tgisinternal;
  IF v_source_triggers <> 0 THEN
    RAISE EXCEPTION 'Tough Temple media rollback refused: media_source has user triggers; use the reviewed trigger-aware rollback path';
  END IF;

  SELECT count(*) INTO v_count
    FROM public.gyms
   WHERE id = '52040e09-986f-43d6-a60d-306fa8e234fe'::uuid
     AND name = 'Tough Temple';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'Tough Temple media rollback refused: registry UUID/name evidence changed';
  END IF;
  SELECT count(*) INTO v_count
    FROM public.echo_intake_tokens
   WHERE gym_id = '52040e09-986f-43d6-a60d-306fa8e234fe'::uuid
     AND echo_account_key = 'toughtemple52040e';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'Tough Temple media rollback refused: current portal-key mapping changed';
  END IF;
  SELECT count(*) INTO v_count
    FROM public.echo_intake_tokens
   WHERE echo_account_key = 'toughtemple086f51';
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'Tough Temple media rollback refused: stale key is now registered';
  END IF;
  SELECT count(*) INTO v_count
    FROM public.media_source
   WHERE gym_id = 'toughtemple52040e'
     AND id <> '389f7e3157514588b8598784b60f9123';
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'Tough Temple media rollback refused: another source now uses the current key';
  END IF;
  SELECT count(*) INTO v_count
    FROM public.media_source
   WHERE gym_id = 'toughtemple086f51'
     AND id <> '389f7e3157514588b8598784b60f9123';
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'Tough Temple media rollback refused: another source uses the stale alias';
  END IF;

  SELECT count(*),
         count(*) FILTER (WHERE kind = 'photo'),
         count(*) FILTER (WHERE kind = 'video'),
         count(*) FILTER (WHERE gym_id <> 'toughtemple52040e')
    INTO v_assets, v_photos, v_videos, v_bad_assets
    FROM public.media_asset
   WHERE source_id = '389f7e3157514588b8598784b60f9123';
  IF v_assets <> 82 OR v_photos <> 18 OR v_videos <> 64 OR v_bad_assets <> 0 THEN
    RAISE EXCEPTION 'Tough Temple media rollback refused: linked asset manifest changed (total %, photos %, videos %, other tenant %)',
      v_assets, v_photos, v_videos, v_bad_assets;
  END IF;
  SELECT count(*) INTO v_count
    FROM public.media_asset
   WHERE gym_id = 'toughtemple086f51';
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'Tough Temple media rollback refused: any asset is already under stale alias (%)', v_count;
  END IF;
  SELECT count(*) INTO v_bad_calendar
    FROM public.content_calendar c
    JOIN public.media_asset a ON a.id = c.source_media_asset_id
   WHERE a.source_id = '389f7e3157514588b8598784b60f9123'
     AND c.gym_id <> 'toughtemple52040e';
  IF v_bad_calendar <> 0 THEN
    RAISE EXCEPTION 'Tough Temple media rollback refused: calendar ownership changed (%)', v_bad_calendar;
  END IF;

  IF v_source.gym_id = 'toughtemple52040e' THEN
    UPDATE public.media_source
       SET gym_id = 'toughtemple086f51'
     WHERE id = '389f7e3157514588b8598784b60f9123'
       AND gym_id = 'toughtemple52040e'
       AND kind = 'gym_drive'
       AND active IS TRUE
       AND revoked_externally IS FALSE
       AND sync_status = 'idle'
       AND sync_requested_at IS NULL
       AND sync_claim_token IS NULL;
    GET DIAGNOSTICS v_changed = ROW_COUNT;
    IF v_changed <> 1 THEN
      RAISE EXCEPTION 'Tough Temple media rollback refused: compare-and-set did not update exactly one row';
    END IF;
  END IF;
  SELECT count(*) INTO v_count
    FROM public.media_source
   WHERE id = '389f7e3157514588b8598784b60f9123'
     AND gym_id = 'toughtemple086f51';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'Tough Temple media rollback refused: stale-key postcondition failed';
  END IF;
  RAISE NOTICE 'Tough Temple source restored to its prior stale key; service remains fail-closed';
END
$rollback$;

COMMIT;
