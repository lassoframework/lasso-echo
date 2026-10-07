-- One-time history hold applied to production on 2026-10-07; retained for audit.
-- The 136 legacy clean photos were approved from exact moderation evidence, but
-- that does not prove they were never published before the claim ledger existed.
-- No historical-use clearance is inferred from used_count=0. Keep them out of
-- future selection until a separate owner-signed original-history clearance.
-- This changes technical eligibility/reason only; approval, moderation,
-- consent diagnostics and audit events remain intact. Do not replay blindly.

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
LOCK TABLE public.media_asset, public.content_calendar
  IN SHARE ROW EXCLUSIVE MODE;

DO $hold$
DECLARE
  v_count integer;
  v_future integer;
BEGIN
  SELECT count(*) INTO v_count
    FROM public.media_asset
   WHERE review_note = 'Reconciled legacy clean moderation evidence'
     AND review_status = 'approved'
     AND moderation_status = 'clean'
     AND review_content_hash = content_hash
     AND eligible IS TRUE
     AND reject_reason IS NULL;
  IF v_count <> 136 THEN
    RAISE EXCEPTION 'history hold refused: exact clean cohort changed (%)', v_count;
  END IF;

  SELECT count(*) INTO v_future
    FROM public.content_calendar c
    JOIN public.media_asset a ON a.id = c.source_media_asset_id
   WHERE a.review_note = 'Reconciled legacy clean moderation evidence'
     AND c.scheduled_at >= now()
     AND c.published_at IS NULL
     AND c.status NOT IN ('archived', 'killed', 'denied', 'cancelled');
  IF v_future <> 0 THEN
    RAISE EXCEPTION 'history hold refused: % future cards require separate guarded holds', v_future;
  END IF;

  UPDATE public.media_asset
     SET eligible = false,
         reject_reason = 'historical_use_unresolved'
   WHERE review_note = 'Reconciled legacy clean moderation evidence'
     AND review_status = 'approved'
     AND moderation_status = 'clean'
     AND review_content_hash = content_hash
     AND eligible IS TRUE
     AND reject_reason IS NULL;
  GET DIAGNOSTICS v_count = ROW_COUNT;
  IF v_count <> 136 THEN
    RAISE EXCEPTION 'history hold refused: updated %', v_count;
  END IF;
END
$hold$;

COMMIT;
