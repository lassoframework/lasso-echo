-- Apply before deploying the bind/worker change. Existing sources are not queued
-- automatically; operators can request a specific source through the portal API.
ALTER TABLE media_source ADD COLUMN IF NOT EXISTS sync_status text NOT NULL DEFAULT 'idle'
  CHECK (sync_status IN ('idle', 'queued', 'indexing', 'ready', 'failed'));
ALTER TABLE media_source ADD COLUMN IF NOT EXISTS sync_requested_at timestamptz;
ALTER TABLE media_source ADD COLUMN IF NOT EXISTS sync_started_at timestamptz;
ALTER TABLE media_source ADD COLUMN IF NOT EXISTS sync_finished_at timestamptz;
ALTER TABLE media_source ADD COLUMN IF NOT EXISTS sync_error text;
ALTER TABLE media_source ADD COLUMN IF NOT EXISTS sync_claim_token text;

CREATE INDEX IF NOT EXISTS media_source_sync_pending_idx
  ON media_source (sync_requested_at) WHERE active AND kind = 'gym_drive';

CREATE OR REPLACE FUNCTION request_gym_media_sync(p_source_id text, p_gym_id text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE changed integer;
BEGIN
  UPDATE media_source SET sync_requested_at = clock_timestamp(),
    sync_status = CASE WHEN sync_status = 'indexing' THEN 'indexing' ELSE 'queued' END,
    sync_error = NULL
  WHERE id = p_source_id AND gym_id = p_gym_id AND active AND kind = 'gym_drive';
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed > 0;
END $$;
REVOKE ALL ON FUNCTION request_gym_media_sync(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION request_gym_media_sync(text, text) TO service_role;

CREATE OR REPLACE FUNCTION claim_gym_media_sync()
RETURNS SETOF media_source LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  RETURN QUERY WITH pick AS (
    SELECT id FROM media_source
    WHERE active AND kind = 'gym_drive' AND sync_requested_at IS NOT NULL
      AND (sync_status = 'queued' OR
           (sync_status = 'indexing' AND sync_started_at < now() - interval '3 hours'))
    ORDER BY sync_requested_at LIMIT 1 FOR UPDATE SKIP LOCKED
  ) UPDATE media_source s SET sync_status = 'indexing', sync_started_at = clock_timestamp(),
      sync_claim_token = md5(random()::text || clock_timestamp()::text), sync_error = NULL
    FROM pick WHERE s.id = pick.id RETURNING s.*;
END $$;
REVOKE ALL ON FUNCTION claim_gym_media_sync() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_gym_media_sync() TO service_role;

CREATE OR REPLACE FUNCTION finish_gym_media_sync(p_source_id text, p_token text,
                                                   p_ok boolean, p_error text DEFAULT NULL)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE changed integer;
BEGIN
  UPDATE media_source SET
    sync_status = CASE WHEN sync_requested_at > sync_started_at THEN 'queued'
                       WHEN p_ok THEN 'ready' ELSE 'failed' END,
    sync_finished_at = clock_timestamp(),
    sync_error = CASE WHEN p_ok THEN NULL ELSE left(coalesce(p_error, 'sync failed'), 120) END,
    sync_claim_token = NULL
  WHERE id = p_source_id AND sync_claim_token = p_token AND sync_status = 'indexing';
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed > 0;
END $$;
REVOKE ALL ON FUNCTION finish_gym_media_sync(text, text, boolean, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION finish_gym_media_sync(text, text, boolean, text) TO service_role;
