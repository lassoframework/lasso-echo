-- Additive, intentionally fail-closed until applied by the database operator.
-- Existing approvals have no reviewed byte version and cannot be selected.
ALTER TABLE public.media_asset
  ADD COLUMN IF NOT EXISTS review_content_hash text;

CREATE TABLE IF NOT EXISTS public.media_asset_review_event (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  gym_id text NOT NULL,
  asset_id text NOT NULL,
  content_hash text NOT NULL,
  prior_status text,
  decision text NOT NULL CHECK (decision IN ('approved', 'rejected', 'pending_review')),
  reviewed_by text NOT NULL,
  reviewed_at timestamptz NOT NULL,
  review_note text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS media_asset_review_event_gym_asset_idx
  ON public.media_asset_review_event (gym_id, asset_id, id DESC);

CREATE OR REPLACE FUNCTION public.reject_media_review_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'media review history is append only';
END;
$$;

DROP TRIGGER IF EXISTS media_review_event_no_mutation ON public.media_asset_review_event;
CREATE TRIGGER media_review_event_no_mutation
  BEFORE UPDATE OR DELETE ON public.media_asset_review_event
  FOR EACH ROW EXECUTE FUNCTION public.reject_media_review_event_mutation();

-- The CLI calls this using the service role. Locking the row makes a concurrent
-- Drive content swap and another operator decision serialize. No event is
-- written if the compare-and-swap fails.
CREATE OR REPLACE FUNCTION public.record_gym_media_review(
  p_gym_id text, p_asset_id text, p_expected_hash text,
  p_expected_status text, p_expected_reviewed_at timestamptz,
  p_fields jsonb)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  current_asset public.media_asset%ROWTYPE;
  new_status text;
  new_at timestamptz;
BEGIN
  IF p_gym_id IS NULL OR p_asset_id IS NULL OR
     coalesce(btrim(p_expected_hash), '') = '' OR
     jsonb_typeof(p_fields) <> 'object' THEN
    RETURN false;
  END IF;
  SELECT * INTO current_asset FROM public.media_asset
    WHERE id = p_asset_id AND gym_id = p_gym_id FOR UPDATE;
  IF NOT FOUND OR current_asset.content_hash IS DISTINCT FROM p_expected_hash OR
     current_asset.review_status IS DISTINCT FROM p_expected_status OR
     current_asset.reviewed_at IS DISTINCT FROM p_expected_reviewed_at OR
     (p_fields->>'review_content_hash') IS DISTINCT FROM p_expected_hash THEN
    RETURN false;
  END IF;
  new_status := p_fields->>'review_status';
  IF new_status IS NULL OR
     new_status NOT IN ('approved', 'rejected', 'pending_review') OR
     coalesce(btrim(p_fields->>'reviewed_by'), '') = '' OR
     coalesce(btrim(p_fields->>'reviewed_at'), '') = '' THEN
    RETURN false;
  END IF;
  new_at := (p_fields->>'reviewed_at')::timestamptz;
  UPDATE public.media_asset SET
    review_status = new_status,
    reviewed_by = p_fields->>'reviewed_by',
    reviewed_at = new_at,
    review_note = p_fields->>'review_note',
    review_content_hash = p_expected_hash,
    consent_status = CASE WHEN p_fields ? 'consent_status'
      THEN p_fields->>'consent_status' ELSE consent_status END,
    release_ref = CASE WHEN p_fields ? 'release_ref'
      THEN p_fields->>'release_ref' ELSE release_ref END,
    consent_member_ref = CASE WHEN p_fields ? 'consent_member_ref'
      THEN p_fields->>'consent_member_ref' ELSE consent_member_ref END,
    consent_expires_at = CASE WHEN p_fields ? 'consent_expires_at'
      THEN (p_fields->>'consent_expires_at')::timestamptz
      ELSE consent_expires_at END
    WHERE id = p_asset_id AND gym_id = p_gym_id;
  INSERT INTO public.media_asset_review_event
    (gym_id, asset_id, content_hash, prior_status, decision,
     reviewed_by, reviewed_at, review_note)
  VALUES (p_gym_id, p_asset_id, p_expected_hash, current_asset.review_status,
          new_status, p_fields->>'reviewed_by', new_at, p_fields->>'review_note');
  RETURN true;
END;
$$;

REVOKE ALL ON FUNCTION public.record_gym_media_review(text,text,text,text,timestamptz,jsonb)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.record_gym_media_review(text,text,text,text,timestamptz,jsonb)
  TO service_role;
REVOKE ALL ON public.media_asset_review_event FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT ON public.media_asset_review_event TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.media_asset_review_event_id_seq TO service_role;
