-- DRAFT / UNAPPLIED operator rebind for the proven CrossFit Sunnyside media-source
-- tenant split. This is intentionally NOT in the automatic migration chain.
--
-- Read-only production evidence captured 2026-10-07 in Supabase project
-- ooqcvmcjspeltuuhcvlh (lasso-ops-portal):
--   gym UUID f574c06c-498a-45f8-a599-b2a8863fadfb is "CrossFit Sunnyside";
--   echo_intake_tokens maps that UUID to crossfitsunnysidef574c0;
--   source 10f4bf47d5e24c4086fce5aa916e6768 is active gym_drive under
--     stale derived alias crossfitsunnyside2616ac;
--   all 13 linked assets (9 photos, 4 videos) already use
--     crossfitsunnysidef574c0; no assets use the stale alias;
--   no other source uses either key; all calendar references to those assets
--     use crossfitsunnysidef574c0.
-- The old key is the exact persisted source value, not a recomputed identity.
-- The new key is the sole current portal key for the registered UUID; the
-- source folder and every linked asset already belong to that tenant.
--
-- Effect: update ONLY media_source.gym_id for this exact source row. No media
-- assets, content_calendar rows, approvals, review evidence, or sync status are
-- changed. The rebind restores the existing same-tenant equality checks; it
-- does not add or weaken any runtime guard.
--
-- Apply only after an operator confirms the live project/schema and drains all
-- scheduled and direct sync workers. The short table locks below serialize
-- concurrent database writes during the check/update, but cannot stop a worker
-- that read the old source row before this transaction began and is doing work
-- outside a database transaction. Do not run during an active sync/deploy.
-- Stop if any frozen precondition changed. Do not replace an assertion with a
-- best-effort update.
--
-- Rollback: if no sync or other media writes have occurred after this rebind,
-- run DRAFT_rollback_crossfit_sunnyside_media_source_rebind_20261007.sql. That restores
-- the prior fail-closed split state; it is not a service-restoring rollback.
-- If assets or calendar ownership changed, stop and prepare a new evidence-
-- reviewed repair instead of forcing rollback.

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

-- Keep registry identity and all ownership evidence stable through validation.
LOCK TABLE public.gyms, public.echo_intake_tokens,
           public.media_source, public.media_asset,
           public.content_calendar
  IN SHARE ROW EXCLUSIVE MODE;

DO $rebind$
DECLARE
  v_source public.media_source%ROWTYPE;
  v_count integer;
  v_assets integer;
  v_photos integer;
  v_videos integer;
  v_bad_assets integer;
  v_old_key_assets integer;
  v_calendar_conflicts integer;
  v_source_triggers integer;
  v_asset_digest text;
  v_calendar_digest text;
  v_changed integer;
BEGIN
  SELECT * INTO v_source
    FROM public.media_source
   WHERE id = '10f4bf47d5e24c4086fce5aa916e6768'
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rebind refused: expected source row is missing';
  END IF;
  IF v_source.kind IS DISTINCT FROM 'gym_drive'
     OR v_source.active IS DISTINCT FROM true
     OR v_source.revoked_externally IS DISTINCT FROM false
     OR v_source.folder_id IS DISTINCT FROM '1eyCZrpS37m_ebpX1LbzI1hk_Ma9BAAPV' THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rebind refused: source kind/activity/revocation changed';
  END IF;
  IF v_source.sync_status IS DISTINCT FROM 'idle'
     OR v_source.sync_requested_at IS NOT NULL
     OR v_source.sync_claim_token IS NOT NULL THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rebind refused: sync is queued, claimed, or no longer idle';
  END IF;
  IF v_source.gym_id IS DISTINCT FROM 'crossfitsunnyside2616ac'
     AND v_source.gym_id IS DISTINCT FROM 'crossfitsunnysidef574c0' THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rebind refused: source key is unexpected (%)', v_source.gym_id;
  END IF;
  -- The production schema read for this draft had no user triggers on
  -- media_source. If an ownership/update trigger has since appeared, this
  -- operator script is no longer the verified rebind path; stop for review.
  SELECT count(*) INTO v_source_triggers
    FROM pg_trigger
   WHERE tgrelid = 'public.media_source'::regclass
     AND NOT tgisinternal;
  IF v_source_triggers <> 0 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rebind refused: media_source has user triggers; use the reviewed trigger-aware rebind path';
  END IF;

  SELECT count(*) INTO v_count
    FROM public.gyms
   WHERE id = 'f574c06c-498a-45f8-a599-b2a8863fadfb'::uuid
     AND name = 'CrossFit Sunnyside';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rebind refused: registry UUID/name evidence changed';
  END IF;

  SELECT count(*) INTO v_count
    FROM public.echo_intake_tokens
   WHERE gym_id = 'f574c06c-498a-45f8-a599-b2a8863fadfb'::uuid
     AND echo_account_key = 'crossfitsunnysidef574c0';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rebind refused: expected current portal-key mapping is absent or duplicated';
  END IF;
  SELECT count(*) INTO v_count
    FROM public.echo_intake_tokens
   WHERE echo_account_key IN ('crossfitsunnysidef574c0', 'crossfitsunnyside2616ac')
     AND gym_id <> 'f574c06c-498a-45f8-a599-b2a8863fadfb'::uuid;
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rebind refused: one of the keys is registered to another gym';
  END IF;
  SELECT count(*) INTO v_count
    FROM public.echo_intake_tokens
   WHERE gym_id = 'f574c06c-498a-45f8-a599-b2a8863fadfb'::uuid
     AND echo_account_key <> 'crossfitsunnysidef574c0';
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rebind refused: target gym has an additional registered key';
  END IF;

  -- The source id is the only allowed source under the stale alias. A source
  -- already attached under the current key would make this one-time repair
  -- ambiguous and requires a separate review.
  SELECT count(*) INTO v_count
    FROM public.media_source
   WHERE gym_id = 'crossfitsunnyside2616ac'
     AND id <> '10f4bf47d5e24c4086fce5aa916e6768';
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rebind refused: another source uses the stale alias';
  END IF;
  SELECT count(*) INTO v_count
    FROM public.media_source
   WHERE gym_id = 'crossfitsunnysidef574c0'
     AND id <> '10f4bf47d5e24c4086fce5aa916e6768';
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rebind refused: another source already uses the current key';
  END IF;

  -- Freeze the audited child set. Only the source row is re-keyed; all linked
  -- assets must already be on the registered portal key and nowhere else.
  SELECT count(*),
         count(*) FILTER (WHERE kind = 'photo'),
         count(*) FILTER (WHERE kind = 'video'),
         count(*) FILTER (WHERE gym_id IS DISTINCT FROM 'crossfitsunnysidef574c0')
    INTO v_assets, v_photos, v_videos, v_bad_assets
    FROM public.media_asset
   WHERE source_id = '10f4bf47d5e24c4086fce5aa916e6768';
  IF v_assets <> 13 OR v_photos <> 9 OR v_videos <> 4 OR v_bad_assets <> 0 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rebind refused: linked asset manifest changed (total %, photos %, videos %, other tenant %)',
      v_assets, v_photos, v_videos, v_bad_assets;
  END IF;
  SELECT md5(string_agg(row_to_json(a)::text, '|' ORDER BY a.id))
    INTO v_asset_digest
    FROM public.media_asset a
   WHERE a.source_id = '10f4bf47d5e24c4086fce5aa916e6768';
  IF v_asset_digest IS DISTINCT FROM '2fd3111504be27cefed69daaca1d7482' THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rebind refused: linked asset snapshot changed';
  END IF;
  SELECT count(*) INTO v_old_key_assets
    FROM public.media_asset
   WHERE gym_id = 'crossfitsunnyside2616ac';
  IF v_old_key_assets <> 0 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rebind refused: assets still exist under stale alias (%)', v_old_key_assets;
  END IF;

  SELECT count(*) INTO v_calendar_conflicts
    FROM public.content_calendar c
    JOIN public.media_asset a ON a.id = c.source_media_asset_id
   WHERE a.source_id = '10f4bf47d5e24c4086fce5aa916e6768'
     AND c.gym_id IS DISTINCT FROM 'crossfitsunnysidef574c0';
  IF v_calendar_conflicts <> 0 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rebind refused: calendar references cross tenant (%)', v_calendar_conflicts;
  END IF;
  SELECT md5(string_agg(row_to_json(c)::text, '|' ORDER BY c.id))
    INTO v_calendar_digest
    FROM public.content_calendar c
    JOIN public.media_asset a ON a.id = c.source_media_asset_id
   WHERE a.source_id = '10f4bf47d5e24c4086fce5aa916e6768';
  IF v_calendar_digest IS DISTINCT FROM '9c7888892cd3cb3ce886cd6ca8e63561' THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rebind refused: calendar snapshot changed';
  END IF;

  -- Safe repeat: accept only the fully verified post-state; otherwise perform
  -- exactly one compare-and-set update from the frozen stale key.
  IF v_source.gym_id = 'crossfitsunnyside2616ac' THEN
    UPDATE public.media_source
       SET gym_id = 'crossfitsunnysidef574c0'
     WHERE id = '10f4bf47d5e24c4086fce5aa916e6768'
       AND gym_id = 'crossfitsunnyside2616ac'
       AND kind = 'gym_drive'
       AND active IS TRUE
       AND revoked_externally IS FALSE
       AND folder_id = '1eyCZrpS37m_ebpX1LbzI1hk_Ma9BAAPV'
       AND sync_status = 'idle'
       AND sync_requested_at IS NULL
       AND sync_claim_token IS NULL;
    GET DIAGNOSTICS v_changed = ROW_COUNT;
    IF v_changed <> 1 THEN
      RAISE EXCEPTION 'CrossFit Sunnyside media rebind refused: compare-and-set did not update exactly one row';
    END IF;
  END IF;

  SELECT count(*) INTO v_count
    FROM public.media_source
   WHERE id = '10f4bf47d5e24c4086fce5aa916e6768'
     AND gym_id = 'crossfitsunnysidef574c0'
     AND kind = 'gym_drive'
     AND active IS TRUE
     AND revoked_externally IS FALSE
     AND folder_id = '1eyCZrpS37m_ebpX1LbzI1hk_Ma9BAAPV'
     AND sync_status = 'idle'
     AND sync_requested_at IS NULL
     AND sync_claim_token IS NULL;
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'CrossFit Sunnyside media rebind refused: canonical postcondition failed';
  END IF;

  RAISE NOTICE 'CrossFit Sunnyside media source is bound to its registered portal key; assets, calendar rows, approvals, and sync status were not modified';
END
$rebind$;

COMMIT;
