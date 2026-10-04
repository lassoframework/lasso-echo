-- Logical post identity foundation (2026-10-04, security-reviewed revision).
--
-- A "logical post" is one IG feed, its FB mirror, and its paired caption-burned
-- Story: one content idea expressed across channels. Independent review found no
-- authoritative same-post identity on content_calendar, so new sibling rows may
-- carry a single shared logical_post_id (minted once upstream, never per row).
--
-- This migration is ADDITIVE and FORWARD-ONLY: one nullable uuid column, one
-- (gym_id, logical_post_id) index, and an UPDATE-trigger immutability guard.
-- There is deliberately NO backfill path here: a security review rejected the
-- earlier audited-assignment design because it relied on a user-settable custom
-- GUC (app.logical_post_backfill) that any updater could set to bypass the
-- guard, and on a security-definer RPC that service_role could use to forge
-- audit rows. That mechanism is removed entirely. Historical NULL rows remain
-- NULL; any backfill will be a separate provenance-reviewed operator migration
-- applied under the normal migration gate. Do not apply to production without
-- that gate.
--
-- Guarantees after apply:
--   * INSERTs may carry a logical_post_id (new sibling groups minted upstream).
--   * UPDATEs may NEVER set, change, or clear logical_post_id in any direction:
--     NULL-to-UUID, UUID-to-other-UUID, and UUID-to-NULL are all refused by
--     NEW IS DISTINCT FROM OLD. Ordinary column updates and re-asserting the
--     same value are permitted.
--   * No RPC, audit table, custom GUC, or special grant exists to bypass the
--     guard; there is nothing to forge and nothing to leak across statements.

begin;

alter table public.content_calendar
  add column if not exists logical_post_id uuid;

create index if not exists content_calendar_gym_logical_post_id_idx
  on public.content_calendar (gym_id, logical_post_id);

create or replace function public.content_calendar_logical_post_guard()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if new.logical_post_id is distinct from old.logical_post_id then
    raise exception 'logical_post_id cannot be set or changed by UPDATE (row %)', old.id;
  end if;
  return new;
end;
$$;

drop trigger if exists content_calendar_logical_post_guard on public.content_calendar;
create trigger content_calendar_logical_post_guard
  before update on public.content_calendar
  for each row execute function public.content_calendar_logical_post_guard();

commit;
