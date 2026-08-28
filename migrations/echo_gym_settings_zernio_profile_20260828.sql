-- Account-key canonical fix (Bird Dog CrossFit / Bolton Club, live, 2026-08-28):
-- give the SHARED plane a real home for the Zernio profile binding.
--
-- THE PHANTOM COLUMN: _persist_profile_id / set_gym_zernio_profile_id WRITE, and
-- gym_zernio_profile_id READS, echo_gym_settings.zernio_profile_id — but that column
-- never existed (live echo_gym_settings is only gym_id, autonomous, autonomy_updated_by,
-- updated_at, posts_per_day, cadence_updated_by). So the write silently 400'd / no-op'd
-- and the volume-less echo-intake-web service could never read the profile id: status
-- fell back to find-by-name every time. The Zernio profile id therefore lived ONLY in the
-- worker's local SQLite, invisible cross-service.
--
-- This migration adds the two columns the code already expects, so the gym -> Zernio
-- profile bind persists on the SHARED plane and BOTH services read it. Additive only,
-- nullable, no default behaviour change. Apply to the Supabase project, then the existing
-- set_gym_zernio_profile_id / gym_zernio_profile_id calls become real cross-service writes/reads.
alter table public.echo_gym_settings
  add column if not exists zernio_profile_id text;
alter table public.echo_gym_settings
  add column if not exists zernio_default_fb_page_id text;
