-- Follow-up for databases that applied media_asset_review_binding_20260918 before
-- its trigger function pinned the schema search path.
ALTER FUNCTION public.reject_media_review_event_mutation()
  SET search_path = public;
