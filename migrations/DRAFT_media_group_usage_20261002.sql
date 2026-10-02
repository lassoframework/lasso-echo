-- DRAFT — NOT APPLIED to any database.  This file is under review for the
-- global no-repeat media repair (Echo visual-group uniqueness across dates).
-- Do not run against production or staging until the DRAFT_ prefix is removed
-- and the runbook in docs/MEDIA_GROUP_RESERVATION.md is signed off.
--
-- Per-gym visual media groups and atomic date-scoped reservations.
--
-- Background: distinct Drive file IDs and distinct MD5 hashes do NOT prove
-- visual uniqueness (four different IDs/MD5s were visually the same scene).
-- Visual identity is therefore expressed as a media_group: a cluster of
-- perceptually-equivalent assets for one gym, identified by a stable
-- group_key derived from dct_phash clustering (agent/vision.py).
--
-- Invariants enforced here:
--   1. Same-date siblings (IG/FB/Story/GBP posts on one usage_date) MAY share
--      one group.
--   2. A group with any active reservation on one date may NOT be reserved
--      for a different date.
--   3. Once any sibling is published, the group is permanently used for that
--      gym: no release, no reuse on any other date, ever.
--   4. A pending/reserved group is released only when its last active sibling
--      reservation is removed.
--   5. Exhausted or unknown groups hold visibly and never recycle: their
--      status is terminal and claim attempts fail closed.
--
-- gym_id is Echo's canonical tenant base/account key (for example ``gymx``),
-- matching media_runway_state conventions.  All access is service-role only;
-- browser roles have no table access and there are no RLS policies.

-- --------------------------------------------------------------------------
-- Tables
-- --------------------------------------------------------------------------

create table if not exists public.media_group (
  gym_id text not null check (nullif(btrim(gym_id), '') is not null),
  -- Stable cluster key, e.g. 'mg_' || sha256(gym_id || ':' || centroid)[:24].
  group_key text not null check (nullif(btrim(group_key), '') is not null),
  -- 64-bit dct pHash centroid as 16 lowercase hex chars (agent/vision.py
  -- dct_phash output), the cluster's representative hash.
  phash_centroid text check (
    phash_centroid is null
    or phash_centroid ~ '^[0-9a-f]{16}$'
  ),
  -- Member hashes and provenance: jsonb arrays, never null, always the right
  -- shape so consumers never guess.  source_lineage_ids carries the original
  -- Drive file IDs / asset IDs that rolled up into this group.
  member_phashes jsonb not null default '[]'::jsonb
    check (jsonb_typeof(member_phashes) = 'array'),
  source_lineage_ids jsonb not null default '[]'::jsonb
    check (jsonb_typeof(source_lineage_ids) = 'array'),
  -- active     : claimable.
  -- exhausted  : library identity depleted for this cluster; holds visibly,
  --              terminal, never claimable again.
  -- unknown    : visual identity could not be established; holds visibly,
  --              terminal, fail closed.
  -- retired    : administratively withdrawn; terminal.
  status text not null default 'active' check (
    status in ('active', 'exhausted', 'unknown', 'retired')
  ),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (gym_id, group_key)
);

comment on table public.media_group is
  'Server-only per-gym visual media group: a cluster of perceptually equivalent assets keyed by dct pHash centroid. Status transitions to exhausted/unknown/retired are terminal; exhausted and unknown groups fail closed on every claim. Contains no token, credential, or client-write state.';

create table if not exists public.media_group_usage (
  gym_id text not null,
  group_key text not null,
  -- The calendar date the sibling post targets (UTC date of the slot).
  usage_date date not null,
  -- Stable id of the sibling post (portal calendar row id / event id), so
  -- concurrent same-date sibling claims are idempotent per sibling.
  sibling_id text not null check (nullif(btrim(sibling_id), '') is not null),
  -- ig | fb | story | gbp (free text, but constrained to the known lanes so a
  -- typo cannot silently fragment sibling accounting).
  channel text not null check (channel in ('ig', 'fb', 'story', 'gbp')),
  -- reserved  : pending claim; releasable.
  -- published : permanently used; terminal for this row.
  status text not null default 'reserved' check (status in ('reserved', 'published')),
  published_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (gym_id, group_key, usage_date, sibling_id),
  foreign key (gym_id, group_key)
    references public.media_group (gym_id, group_key)
    on delete restrict
);

comment on table public.media_group_usage is
  'Server-only date-scoped reservation/usage ledger for media groups. Same-date siblings may share a group; a published row makes the group permanently used for the gym on all other dates. Rows are deleted only to release reservations; published rows are never deleted.';

-- Block any deletion of a published usage row and any mutation that reopens a
-- published row, and block terminal-status recycling on media_group.  This is
-- defense in depth under the RPCs below, which are the only write path.
create or replace function public.media_group_usage_guard()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if tg_op = 'DELETE' and old.status = 'published' then
    raise exception 'published media group usage is permanent and cannot be removed';
  end if;
  if tg_op = 'UPDATE' and old.status = 'published' and new.status <> 'published' then
    raise exception 'published media group usage cannot be reopened';
  end if;
  if tg_op = 'UPDATE' then
    new.updated_at := now();
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end
$$;

drop trigger if exists media_group_usage_guard on public.media_group_usage;
create trigger media_group_usage_guard
before update or delete on public.media_group_usage
for each row execute function public.media_group_usage_guard();

create or replace function public.media_group_status_guard()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  -- Terminal statuses never recycle.  active -> terminal is allowed; any
  -- transition out of a terminal status is rejected.
  if old.status in ('exhausted', 'unknown', 'retired') and new.status <> old.status then
    raise exception 'media group status % is terminal and cannot change', old.status;
  end if;
  new.updated_at := now();
  return new;
end
$$;

drop trigger if exists media_group_status_guard on public.media_group;
create trigger media_group_status_guard
before update on public.media_group
for each row execute function public.media_group_status_guard();

alter table public.media_group enable row level security;
alter table public.media_group_usage enable row level security;

-- Browser roles have no table access and there are no policies.  Only Echo's
-- server-side service role reads these tables; every write goes through the
-- RPCs below.
revoke all on table public.media_group from public, anon, authenticated;
revoke all on table public.media_group_usage from public, anon, authenticated;
revoke insert, update, delete on table public.media_group from service_role;
revoke insert, update, delete on table public.media_group_usage from service_role;
grant select on table public.media_group to service_role;
grant select on table public.media_group_usage to service_role;

-- --------------------------------------------------------------------------
-- Atomic claim: reserve a group for one sibling on one date.
--
-- Serialization is per (gym_id, group_key): the group row is locked
-- SELECT ... FOR UPDATE before any usage row is touched, so concurrent
-- same-date sibling claims serialize cleanly and both succeed, while a
-- cross-date claim always observes the existing active reservation and fails.
--
-- Returns one row describing the outcome instead of raising, so the caller
-- can fail closed with a visible hold reason rather than an exception:
--   claimed       : reservation recorded (or already held by this sibling).
--   shared        : joined existing same-date sibling set.
--   held_terminal : group status is exhausted/unknown/retired (fail closed).
--   held_used     : group already published (same or other date).
--   held_conflict : group actively reserved on a different date.
--   missing_group : no such group for this gym (unknown identity: fail closed).
-- --------------------------------------------------------------------------

create or replace function public.claim_media_group(
  p_gym_id text,
  p_group_key text,
  p_usage_date date,
  p_sibling_id text,
  p_channel text
)
returns table (
  outcome text,
  gym_id text,
  group_key text,
  usage_date date,
  sibling_id text,
  status text
)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_group public.media_group%rowtype;
  v_published_exists boolean;
  v_other_date_reserved boolean;
  v_same_date_exists boolean;
begin
  if nullif(btrim(p_gym_id), '') is null or p_gym_id <> btrim(p_gym_id)
      or nullif(btrim(p_group_key), '') is null or p_group_key <> btrim(p_group_key)
      or nullif(btrim(p_sibling_id), '') is null or p_sibling_id <> btrim(p_sibling_id)
      or p_usage_date is null
      or p_channel not in ('ig', 'fb', 'story', 'gbp') then
    raise exception 'invalid media group claim arguments';
  end if;

  select g.* into v_group
    from public.media_group as g
   where g.gym_id = p_gym_id and g.group_key = p_group_key
   for update of g;

  if not found then
    -- Unknown identity holds visibly and never recycles: no row, no claim.
    return query select 'missing_group'::text, p_gym_id, p_group_key,
                        p_usage_date, p_sibling_id, null::text;
    return;
  end if;

  if v_group.status <> 'active' then
    return query select 'held_terminal'::text, v_group.gym_id, v_group.group_key,
                        p_usage_date, p_sibling_id, v_group.status;
    return;
  end if;

  select exists (
    select 1 from public.media_group_usage as u
     where u.gym_id = p_gym_id and u.group_key = p_group_key
       and u.status = 'published'
  ) into v_published_exists;

  select exists (
    select 1 from public.media_group_usage as u
     where u.gym_id = p_gym_id and u.group_key = p_group_key
       and u.status = 'reserved' and u.usage_date <> p_usage_date
  ) into v_other_date_reserved;

  select exists (
    select 1 from public.media_group_usage as u
     where u.gym_id = p_gym_id and u.group_key = p_group_key
       and u.usage_date = p_usage_date
  ) into v_same_date_exists;

  -- Published anywhere in this gym's history for this group is permanent:
  -- same-date siblings of a published group may share it, every other date
  -- is refused.
  if v_published_exists and not v_same_date_exists then
    return query select 'held_used'::text, v_group.gym_id, v_group.group_key,
                        p_usage_date, p_sibling_id, 'published'::text;
    return;
  end if;

  if v_other_date_reserved then
    return query select 'held_conflict'::text, v_group.gym_id, v_group.group_key,
                        p_usage_date, p_sibling_id, 'reserved'::text;
    return;
  end if;

  insert into public.media_group_usage as u (
    gym_id, group_key, usage_date, sibling_id, channel, status
  ) values (
    p_gym_id, p_group_key, p_usage_date, p_sibling_id, p_channel, 'reserved'
  )
  on conflict on constraint media_group_usage_pkey do nothing;

  return query select
      case when v_same_date_exists then 'shared' else 'claimed' end,
      p_gym_id, p_group_key, p_usage_date, p_sibling_id,
      'reserved'::text;
end
$$;

-- Mark a sibling (or a whole same-date sibling set) published.  Permanent:
-- the guard trigger forbids reopening or deleting these rows, and the claim
-- RPC refuses any other date for the group forever after.
create or replace function public.publish_media_group_usage(
  p_gym_id text,
  p_group_key text,
  p_usage_date date,
  p_sibling_id text default null
)
returns integer
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_count integer;
begin
  if nullif(btrim(p_gym_id), '') is null or p_gym_id <> btrim(p_gym_id)
      or nullif(btrim(p_group_key), '') is null or p_group_key <> btrim(p_group_key)
      or p_usage_date is null then
    raise exception 'invalid media group publish arguments';
  end if;

  update public.media_group_usage as u
     set status = 'published',
         published_at = coalesce(u.published_at, now())
   where u.gym_id = p_gym_id
     and u.group_key = p_group_key
     and u.usage_date = p_usage_date
     and u.status = 'reserved'
     and (p_sibling_id is null or u.sibling_id = p_sibling_id);

  get diagnostics v_count = row_count;
  return v_count;
end
$$;

-- Release a pending reservation for one sibling.  The group becomes reusable
-- for any date only once its last active sibling reservation is removed;
-- this function deletes only reserved rows (published rows are untouchable),
-- so "released after last sibling" is simply the moment no rows remain.
-- Returns the number of reservations still active for the group afterwards,
-- so the caller can see whether the group is fully released.
create or replace function public.release_media_group_reservation(
  p_gym_id text,
  p_group_key text,
  p_usage_date date,
  p_sibling_id text
)
returns integer
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_remaining integer;
begin
  if nullif(btrim(p_gym_id), '') is null or p_gym_id <> btrim(p_gym_id)
      or nullif(btrim(p_group_key), '') is null or p_group_key <> btrim(p_group_key)
      or nullif(btrim(p_sibling_id), '') is null or p_sibling_id <> btrim(p_sibling_id)
      or p_usage_date is null then
    raise exception 'invalid media group release arguments';
  end if;

  delete from public.media_group_usage as u
   where u.gym_id = p_gym_id
     and u.group_key = p_group_key
     and u.usage_date = p_usage_date
     and u.sibling_id = p_sibling_id
     and u.status = 'reserved';

  select count(*) into v_remaining
    from public.media_group_usage as u
   where u.gym_id = p_gym_id and u.group_key = p_group_key;

  return v_remaining;
end
$$;

-- Administrative terminal hold for exhausted/unknown identity.  One-way by
-- trigger; service-role only.  Returns false when the group does not exist or
-- is already terminal (no-op hold, still visible).
create or replace function public.hold_media_group(
  p_gym_id text,
  p_group_key text,
  p_status text
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if nullif(btrim(p_gym_id), '') is null or p_gym_id <> btrim(p_gym_id)
      or nullif(btrim(p_group_key), '') is null or p_group_key <> btrim(p_group_key)
      or p_status not in ('exhausted', 'unknown', 'retired') then
    raise exception 'invalid media group hold arguments';
  end if;

  update public.media_group as g
     set status = p_status
   where g.gym_id = p_gym_id
     and g.group_key = p_group_key
     and g.status = 'active';

  return found;
end
$$;

revoke all on function public.media_group_usage_guard() from public, anon, authenticated;
revoke all on function public.media_group_status_guard() from public, anon, authenticated;
revoke all on function public.claim_media_group(text, text, date, text, text)
  from public, anon, authenticated;
revoke all on function public.publish_media_group_usage(text, text, date, text)
  from public, anon, authenticated;
revoke all on function public.release_media_group_reservation(text, text, date, text)
  from public, anon, authenticated;
revoke all on function public.hold_media_group(text, text, text)
  from public, anon, authenticated;

grant execute on function public.claim_media_group(text, text, date, text, text)
  to service_role;
grant execute on function public.publish_media_group_usage(text, text, date, text)
  to service_role;
grant execute on function public.release_media_group_reservation(text, text, date, text)
  to service_role;
grant execute on function public.hold_media_group(text, text, text)
  to service_role;
