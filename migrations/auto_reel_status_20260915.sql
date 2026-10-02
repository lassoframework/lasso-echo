-- Cross-service read model only. Echo's volume worker remains the job authority.
CREATE TABLE IF NOT EXISTS public.auto_reel_status (
  gym_id text PRIMARY KEY,
  snapshot jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public.auto_reel_status ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.auto_reel_status FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON public.auto_reel_status TO service_role;
