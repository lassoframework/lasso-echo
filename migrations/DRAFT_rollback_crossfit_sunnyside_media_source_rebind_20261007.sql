-- DRAFT / UNAPPLIED rollback for
-- DRAFT_crossfit_sunnyside_media_source_rebind_20261007.sql.
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
   WHERE id = '10f4bf47d5e24c4086fce5aa916e6768'
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rollback refused: expected source row is missing';
  END IF;
  IF v_source.gym_id NOT IN ('crossfitsunnysidef574c0', 'crossfitsunnyside2616ac')
     OR v_source.kind IS DISTINCT FROM 'gym_drive'
     OR v_source.active IS DISTINCT FROM true
     OR v_source.revoked_externally IS DISTINCT FROM false
     OR v_source.sync_status IS DISTINCT FROM 'idle'
     OR v_source.sync_requested_at IS NOT NULL
     OR v_source.sync_claim_token IS NOT NULL THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rollback refused: source state changed or work is queued';
  END IF;
  SELECT count(*) INTO v_source_triggers
    FROM pg_trigger
   WHERE tgrelid = 'public.media_source'::regclass
     AND NOT tgisinternal;
  IF v_source_triggers <> 0 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rollback refused: media_source has user triggers; use the reviewed trigger-aware rollback path';
  END IF;

  SELECT count(*) INTO v_count
    FROM public.gyms
   WHERE id = 'f574c06c-498a-45f8-a599-b2a8863fadfb'::uuid
     AND name = 'CrossFit Sunnyside';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rollback refused: registry UUID/name evidence changed';
  END IF;
  SELECT count(*) INTO v_count
    FROM public.echo_intake_tokens
   WHERE gym_id = 'f574c06c-498a-45f8-a599-b2a8863fadfb'::uuid
     AND echo_account_key = 'crossfitsunnysidef574c0';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rollback refused: current portal-key mapping changed';
  END IF;
  SELECT count(*) INTO v_count
    FROM public.echo_intake_tokens
   WHERE echo_account_key = 'crossfitsunnyside2616ac';
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rollback refused: stale key is now registered';
  END IF;
  SELECT count(*) INTO v_count
    FROM public.media_source
   WHERE gym_id = 'crossfitsunnysidef574c0'
     AND id <> '10f4bf47d5e24c4086fce5aa916e6768';
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rollback refused: another source now uses the current key';
  END IF;
  SELECT count(*) INTO v_count
    FROM public.media_source
   WHERE gym_id = 'crossfitsunnyside2616ac'
     AND id <> '10f4bf47d5e24c4086fce5aa916e6768';
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rollback refused: another source uses the stale alias';
  END IF;

  SELECT count(*),
         count(*) FILTER (WHERE kind = 'photo'),
         count(*) FILTER (WHERE kind = 'video'),
         count(*) FILTER (WHERE gym_id <> 'crossfitsunnysidef574c0')
    INTO v_assets, v_photos, v_videos, v_bad_assets
    FROM public.media_asset
   WHERE source_id = '10f4bf47d5e24c4086fce5aa916e6768';
  IF v_assets <> 13 OR v_photos <> 9 OR v_videos <> 4 OR v_bad_assets <> 0 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rollback refused: linked asset manifest changed (total %, photos %, videos %, other tenant %)',
      v_assets, v_photos, v_videos, v_bad_assets;
  END IF;
  SELECT count(*) INTO v_count
    FROM public.media_asset
   WHERE gym_id = 'crossfitsunnyside2616ac';
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rollback refused: any asset is already under stale alias (%)', v_count;
  END IF;
  SELECT count(*) INTO v_bad_calendar
    FROM public.content_calendar c
    JOIN public.media_asset a ON a.id = c.source_media_asset_id
   WHERE a.source_id = '10f4bf47d5e24c4086fce5aa916e6768'
     AND c.gym_id <> 'crossfitsunnysidef574c0';
  IF v_bad_calendar <> 0 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rollback refused: calendar ownership changed (%)', v_bad_calendar;
  END IF;

  IF v_source.gym_id = 'crossfitsunnysidef574c0' THEN
    UPDATE public.media_source
       SET gym_id = 'crossfitsunnyside2616ac'
     WHERE id = '10f4bf47d5e24c4086fce5aa916e6768'
       AND gym_id = 'crossfitsunnysidef574c0'
       AND kind = 'gym_drive'
       AND active IS TRUE
       AND revoked_externally IS FALSE
       AND sync_status = 'idle'
       AND sync_requested_at IS NULL
       AND sync_claim_token IS NULL;
    GET DIAGNOSTICS v_changed = ROW_COUNT;
    IF v_changed <> 1 THEN
      RAISE EXCEPTION 'CrossFit Sunnyside media rollback refused: compare-and-set did not update exactly one row';
    END IF;
  END IF;
  SELECT count(*) INTO v_count
    FROM public.media_source
   WHERE id = '10f4bf47d5e24c4086fce5aa916e6768'
     AND gym_id = 'crossfitsunnyside2616ac';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rollback refused: stale-key postcondition failed';
  END IF;
  RAISE NOTICE 'CrossFit Sunnyside source restored to its prior stale key; service remains fail-closed';
END
$rollback$;

COMMIT;
