-- The append-only review ledger is service-role only. RLS adds a second
-- deny-by-default boundary for every browser-facing role.
ALTER TABLE public.media_asset_review_event ENABLE ROW LEVEL SECURITY;
