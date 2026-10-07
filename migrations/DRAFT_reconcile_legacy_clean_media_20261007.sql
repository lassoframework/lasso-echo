-- DRAFT / UNAPPLIED one-time reconciliation of legacy clean Drive photos.
-- Current Echo moderation automatically approves a clean, byte-bound verdict.
-- These 136 older rows have that verdict but predate the automatic status write.
-- This script changes review fields only after the entire frozen set and every
-- source/evidence binding are checked under table locks. Do not weaken counts or
-- digests if production has drifted; capture and independently review a new set.
-- Consent columns are legacy diagnostics: current selector does not gate a
-- clean asset on releases. Preserve those fields exactly; this repair does not
-- attest to an individual member release or rewrite prior diagnostics.

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
LOCK TABLE public.media_source, public.media_asset,
           public.media_asset_review_event IN SHARE ROW EXCLUSIVE MODE;

DO $reconcile$
DECLARE
  v_count integer;
  v_digest text;
BEGIN
  SELECT count(*), md5(string_agg(row_to_json(a)::text, '|'
                                  ORDER BY a.gym_id, a.id))
    INTO v_count, v_digest
    FROM public.media_asset a
   WHERE a.moderation_status = 'clean'
     AND a.review_status = 'pending_review';
  IF v_count <> 136 OR
     v_digest IS DISTINCT FROM 'f9222f094fdcb0dcd7017336e6d2ea0f' THEN
    RAISE EXCEPTION 'legacy clean media reconciliation refused: frozen asset set changed (count %, digest %)',
      v_count, v_digest;
  END IF;

  SELECT count(*) INTO v_count
    FROM public.media_asset a
    LEFT JOIN public.media_source s ON s.id = a.source_id
   WHERE a.moderation_status = 'clean'
     AND a.review_status = 'pending_review'
     AND (
       a.gym_id IN ('crossfitreverb30b5b2', 'train7164ae502')
       AND a.kind = 'photo'
       AND a.eligible IS TRUE
       AND a.excluded_by_coach IS NOT TRUE
       AND a.reviewed_by IS NULL
       AND a.reviewed_at IS NULL
       AND a.review_content_hash IS NULL
       AND NULLIF(btrim(a.content_hash), '') IS NOT NULL
       AND s.gym_id = a.gym_id
       AND s.kind = 'gym_drive'
       AND s.active IS TRUE
       AND s.revoked_externally IS FALSE
       AND s.sync_status = 'idle'
       AND s.sync_requested_at IS NULL
       AND s.sync_claim_token IS NULL
       AND a.moderation_json->>'gym_id' = a.gym_id
       AND a.moderation_json->>'asset_id' = a.id
       AND a.moderation_json->>'content_hash' = a.content_hash
       AND a.moderation_json->>'verdict' = 'clean'
       AND NULLIF(btrim(a.moderation_json->>'provider'), '') IS NOT NULL
       AND a.people_detected IS NOT NULL
       AND a.moderation_json->'people_detected' = to_jsonb(a.people_detected)
       AND NULLIF(btrim(a.moderation_json->>'observed_at'), '') IS NOT NULL
     ) IS NOT TRUE;
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'legacy clean media reconciliation refused: % asset/source/evidence rows fail current ownership or moderation proof', v_count;
  END IF;

  WITH promoted AS (
    UPDATE public.media_asset a
       SET review_status = 'approved',
           reviewed_by = 'automatic_moderation',
           reviewed_at = (a.moderation_json->>'observed_at')::timestamptz,
           review_content_hash = a.content_hash,
           review_note = 'Reconciled legacy clean moderation evidence'
     WHERE a.moderation_status = 'clean'
       AND a.review_status = 'pending_review'
    RETURNING a.gym_id, a.id, a.content_hash, a.reviewed_at
  )
  INSERT INTO public.media_asset_review_event
    (gym_id, asset_id, content_hash, prior_status, decision,
     reviewed_by, reviewed_at, review_note)
  SELECT p.gym_id, p.id, p.content_hash, 'pending_review', 'approved',
         'automatic_moderation', p.reviewed_at,
         'Reconciled legacy clean moderation evidence'
    FROM promoted p;
  GET DIAGNOSTICS v_count = ROW_COUNT;
  IF v_count <> 136 THEN
    RAISE EXCEPTION 'legacy clean media reconciliation refused: event/update count % rather than 136', v_count;
  END IF;
  RAISE NOTICE 'Reconciled 136 exact clean legacy photos and wrote 136 review events';
END
$reconcile$;

COMMIT;
