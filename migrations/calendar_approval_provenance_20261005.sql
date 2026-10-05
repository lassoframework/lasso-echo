-- DRAFT (2026-10-05, repaired after independent review, SECOND repair pass):
-- durable exact-content approval provenance for client calendar posts.
-- RECORDING ONLY until AGENT_APPROVAL_PROOF is armed and this migration is
-- independently reviewed and applied.
--
-- Problem: Manual mode (approved_only) publishes any row with status='approved',
-- including rows approved during a prior Automatic period, and nothing binds an
-- approval to the row's exact publish-relevant content. A media/caption/date
-- edit after approval silently publishes under the old approval.
--
-- Repair-pass-2 rulings (reviewer P1 defects):
--   * An Echo bearer token alone must NEVER mint a 'human' proof. Echo's
--     approve path only records status='approved' + the digest of the exact
--     content it served; approval_kind/approved_by stay UNPROVED (NULL).
--     Human-ness is stamped ONLY by calendar_stamp_verified_approval, which
--     the portal calls with its service role AFTER Echo's success, carrying
--     the authenticated Clerk actor and Echo's digest. No body-supplied actor
--     is ever trusted.
--   * The digest binds the FINAL VISIBLE MEDIA (image_url) plus the rendered
--     identity fields, not just the source asset. If feed auto-fit or a story
--     reburn changes image_url after approval, the digest no longer matches
--     and the claim fails CLOSED into fresh review -- changed pixels are never
--     silently published under an old approval. No remote-byte fetching is
--     needed: the bound URL string IS the deterministic identity because the
--     publisher publishes exactly that URL and cannot mutate it after claim.
--
-- This migration:
--   1. Adds approval provenance columns (kind, actor, time, digest) to
--      content_calendar. All existing rows keep NULL provenance = UNPROVED.
--   2. Adds calendar_approval_digest(row): a canonical digest of the exact
--      publish-relevant fields the human approved: account, format, post_date,
--      caption, the FINAL image_url, and the rendered/source identity fields
--      (byte_hash, source_media_asset_id, source_media_url). scheduled_at is
--      EXCLUDED: the publisher itself stamps it AFTER approval (display
--      metadata, a pure function of the row); it is not human-chosen content.
--   3. Redefines approve_calendar_row_if_media_ready (Echo path) as ONE
--      signature (p_row_id, p_gym_id, p_expected jsonb DEFAULT NULL) to stamp
--      status='approved' and the digest of the pre-update row in the SAME
--      atomic UPDATE as the status flip -- and to CLEAR approval_kind,
--      approved_by and approved_at. An Echo-token approval is UNPROVED. The
--      UPDATE also requires variant_status='active' (review defect 3). When
--      the portal sends its visible-card snapshot (p_expected, gated portal-
--      side by PORTAL_ECHO_APPROVAL_PROOF), the SAME UPDATE additionally
--      requires caption/media_url/day_key/format/platform to match the locked
--      row; a stale snapshot updates zero rows (Echo 409s
--      review_refresh_required). p_expected DEFAULT NULL keeps the exact
--      legacy flag-OFF behavior, and the old 2-arg signatures are dropped so
--      PostgREST never sees an overload ambiguity.
--   4. Adds calendar_stamp_verified_approval(p_gym_id uuid, p_calendar_id
--      uuid, p_clerk_actor_id text, p_echo_approval_digest text): the portal's
--      service-role human-proof stamp. It atomically resolves the exact gym
--      (id + exact server-owned token account-key mapping),
--      locks the exact row, requires active/unpublished/approved status, a
--      CURRENT digest equal to both the recomputed digest and Echo's returned
--      digest, and a NONEMPTY Clerk actor, then stamps approval_kind='human',
--      approved_by=actor, approved_at=now(). Returns exactly one row
--      (id, gym_id, clerk_actor_id, approval_digest); any mismatch returns
--      zero rows and nothing is stamped.
--   5. Redefines claim_calendar_publish_slot_owned with an additional
--      p_require_approval_proof boolean (DEFAULT FALSE = zero behavior change
--      for every existing caller). When TRUE, the claim FIRST re-reads the
--      gym's CURRENT autonomy from the authoritative DB (gyms +
--      echo_gym_settings) inside the claim transaction (review defect 4): a
--      gym that is definitively autonomous right now skips the proof gate
--      (autonomous lane unchanged); anything else -- Manual, or an UNRESOLVED/
--      AMBIGUOUS gym/settings lookup -- fails closed and the proof gate is
--      enforced. The proof gate requires approval_kind='human', a NONEMPTY
--      trusted approved_by, approved_at, and a digest matching the LOCKED
--      row's current fields. An Echo-token-only approval (kind NULL) never
--      satisfies it.
--
-- The old overloads are DROPPED first: Postgres treats a defaulted extra
-- parameter as ambiguous with the old signature, and PostgREST would 300 on
-- the overloaded RPC.

alter table public.content_calendar
  add column if not exists approval_kind text,
  add column if not exists approved_by text,
  add column if not exists approved_at timestamptz,
  add column if not exists approval_digest text;

alter table public.content_calendar
  drop constraint if exists content_calendar_approval_kind_check;
alter table public.content_calendar
  add constraint content_calendar_approval_kind_check
  check (approval_kind is null or approval_kind in ('human', 'automatic'));

-- A key must never move to another gym during a proof stamp. Existing
-- duplicates make this migration fail closed; the service must reconcile them
-- before enabling the gate. NULL keys remain unmapped.
create unique index if not exists echo_intake_tokens_echo_account_key_unique
  on public.echo_intake_tokens (echo_account_key);

-- Canonical digest of the exact publish-relevant fields. Field order and
-- normalization are part of the contract: lower/btrim account and format
-- ('feed' default), ISO post_date, raw caption, the FINAL visible image_url,
-- then the rendered/source identity fields byte_hash, source_media_asset_id,
-- source_media_url. Unit-separator joined so field boundaries cannot collide.
-- scheduled_at is deliberately NOT bound (see header); image_url IS bound, so
-- ANY post-approval change of the final media (auto-fit reframe, story
-- reburn, swap) invalidates the proof and the claim fails closed into fresh
-- review. The identity columns are read via to_jsonb so this function is
-- valid whether or not those (separately drafted) columns exist.
create or replace function public.calendar_approval_digest(
  p_row public.content_calendar
) returns text
language sql
stable
set search_path = public
as $$
  select md5(concat_ws(chr(31),
    coalesce(nullif(lower(btrim(p_row.account)), ''), ''),
    coalesce(nullif(lower(btrim(p_row.format)), ''), 'feed'),
    coalesce(p_row.post_date::text, ''),
    coalesce(p_row.caption, ''),
    coalesce(nullif(btrim(p_row.image_url), ''), ''),
    coalesce(nullif(btrim(to_jsonb(p_row)->>'byte_hash'), ''), ''),
    coalesce(nullif(btrim(to_jsonb(p_row)->>'source_media_asset_id'), ''), ''),
    coalesce(nullif(btrim(to_jsonb(p_row)->>'source_media_url'), ''), '')
  ));
$$;

revoke all on function public.calendar_approval_digest(public.content_calendar)
  from public, anon, authenticated;
grant execute on function public.calendar_approval_digest(public.content_calendar)
  to service_role;

-- Approve-and-record in ONE UPDATE (Echo bearer-token path). SET expressions
-- read the PRE-update row, so the digest binds exactly the content the human
-- saw. The digest deliberately excludes status: the claim recomputes it from
-- the locked row regardless. variant_status='active' is REQUIRED: only the
-- live variant may be approved.
--
-- REVIEW DEFECT 1 REPAIR: this RPC does NOT mint a human proof. An Echo
-- bearer token authenticates the gym's approval INTENT, not a verified human
-- identity, so the stamp clears approval_kind/approved_by/approved_at
-- (UNPROVED) and keeps only the status flip + digest. Human provenance is
-- added exclusively by calendar_stamp_verified_approval (portal service-role
-- path, authenticated Clerk actor). There is NO actor parameter: no
-- body-supplied identity is ever trusted here.
-- SINGLE signature (defaulted 3rd arg) so PostgREST never sees an
-- overload ambiguity (a defaulted extra parameter 300s against the old
-- 2-arg signature, and any second overload would too -- so the old
-- signatures are DROPPED, not kept). p_expected DEFAULT NULL reproduces the
-- exact legacy 2-arg behavior: flag OFF, the portal sends no snapshot and
-- this is byte-for-byte the old guarded approve.
drop function if exists public.approve_calendar_row_if_media_ready(uuid, text);
create or replace function public.approve_calendar_row_if_media_ready(
  p_row_id uuid, p_gym_id text, p_expected jsonb default null
) returns setof public.content_calendar
language sql security definer set search_path = public
as $$
  update public.content_calendar c
     set status = 'approved',
         approval_kind = null,
         approved_by = null,
         approved_at = null,
         approval_digest = public.calendar_approval_digest(c)
   where id = p_row_id and gym_id = p_gym_id
     and status = 'pending' and published_at is null
     and late_post_id is null and variant_status = 'active'
     and nullif(btrim(coalesce(image_url, '')), '') is not null
     and media_not_ready_reason is null
     and (
       p_expected is null
       or (
         -- PORTAL VISIBLE-CARD SNAPSHOT COMPARE (ECHO_VERIFIED_APPROVAL_PROOF
         -- CONTRACT): the exact card the human tapped. caption is compared
         -- null-safely and without trimming; media_url is the FINAL image_url
         -- string (no fetching -- the bound URL IS the
         -- identity); day_key is the visible post_date; format is the
         -- effective format ('feed' when unset); platform is the canonical
         -- account platform. ALL of it is checked in THIS SAME UPDATE that
         -- stamps status+digest: a stale snapshot matches zero rows, flips
         -- nothing and stamps nothing -- Echo answers 409
         -- review_refresh_required and the card re-enters review.
         c.caption is not distinct from p_expected->>'caption'
         and c.image_url = p_expected->>'media_url'
         and c.post_date::text
           = coalesce(p_expected->>'day_key', '')
         and coalesce(nullif(lower(btrim(c.format)), ''), 'feed')
           = coalesce(nullif(lower(btrim(coalesce(p_expected->>'format', ''))), ''), 'feed')
         and coalesce(nullif(lower(btrim(c.account)), ''), '')
           = lower(btrim(coalesce(p_expected->>'platform', '')))
       )
     )
  returning *;
$$;

revoke all on function public.approve_calendar_row_if_media_ready(uuid, text, jsonb)
  from public, anon, authenticated;
grant execute on function public.approve_calendar_row_if_media_ready(uuid, text, jsonb)
  to service_role;

-- A failed portal proof stamp leaves an approved but unproved row. A fresh
-- review tap may recover its digest only when the exact current card still
-- matches, the row is unproved, and no publish-relevant field has changed.
-- This RPC never stamps a human actor; the portal still calls the separate
-- service-role stamp with its authenticated Clerk identity.
create or replace function public.calendar_recover_unproved_approval(
  p_row_id uuid, p_gym_id text, p_expected jsonb
) returns setof public.content_calendar
language plpgsql security definer set search_path = public
as $$
declare
  v_row public.content_calendar%rowtype;
begin
  if p_expected is null then return; end if;
  select * into v_row from public.content_calendar c
   where c.id = p_row_id and c.gym_id = p_gym_id
   for update of c;
  if not found or v_row.status is distinct from 'approved'
      or v_row.published_at is not null or v_row.late_post_id is not null
      or v_row.variant_status is distinct from 'active'
      or v_row.approval_kind is not null or v_row.approved_by is not null
      or v_row.approved_at is not null
      or v_row.media_not_ready_reason is not null
      or nullif(btrim(coalesce(v_row.image_url, '')), '') is null
      or v_row.approval_digest is null
      or v_row.approval_digest is distinct from public.calendar_approval_digest(v_row)
      or v_row.caption is distinct from p_expected->>'caption'
      or v_row.image_url is distinct from p_expected->>'media_url'
      or v_row.post_date::text is distinct from p_expected->>'day_key'
      or coalesce(nullif(lower(btrim(v_row.format)), ''), 'feed')
         is distinct from coalesce(nullif(lower(btrim(coalesce(p_expected->>'format', ''))), ''), 'feed')
      or coalesce(nullif(lower(btrim(v_row.account)), ''), '')
         is distinct from lower(btrim(coalesce(p_expected->>'platform', ''))) then
    return;
  end if;
  return next v_row;
end;
$$;
revoke all on function public.calendar_recover_unproved_approval(uuid, text, jsonb)
  from public, anon, authenticated;
grant execute on function public.calendar_recover_unproved_approval(uuid, text, jsonb)
  to service_role;

-- HUMAN PROOF STAMP (portal service-role path, behind the portal's own OFF
-- flag; see docs/ECHO_VERIFIED_APPROVAL_PROOF_CONTRACT.md in the portal
-- worktree). Called only AFTER Echo approved the pending row and returned its
-- approval_digest. Verifies atomically, in one locked transaction:
--   * the gym resolves to EXACTLY ONE clean gyms row by id, and exactly one
--     echo_intake_tokens mapping joins that UUID to the calendar account key;
--     e.g. e5c9db81-... <-> swiftrivercrossfite5c9db. No inferred suffix;
--   * the row is active, unpublished and status='approved';
--   * the row's CURRENT recomputed digest matches the stored digest AND the
--     exact digest Echo returned (any post-approval edit fails closed);
--   * p_clerk_actor_id is NONEMPTY (no actor, no human proof).
-- Stamps approval_kind='human', approved_by=actor, approved_at=now() and
-- returns exactly one row (id, gym_id, clerk_actor_id, approval_digest).
-- Any mismatch returns ZERO rows and stamps nothing.
create or replace function public.calendar_stamp_verified_approval(
  p_gym_id uuid,
  p_calendar_id uuid,
  p_clerk_actor_id text,
  p_echo_approval_digest text
) returns table (id uuid, gym_id uuid, clerk_actor_id text,
                 approval_digest text)
language plpgsql security definer set search_path = public
as $$
declare
  v_actor text := nullif(btrim(coalesce(p_clerk_actor_id, '')), '');
  v_digest text := nullif(btrim(coalesce(p_echo_approval_digest, '')), '');
  v_gym_count integer;
  v_mapping_count integer;
  v_mapped_gym uuid;
  v_row public.content_calendar%rowtype;
begin
  if p_gym_id is null or p_calendar_id is null
      or v_actor is null or v_digest is null then
    return;  -- no trusted actor or no Echo digest: zero rows, no stamp
  end if;

-- Exact gym resolution: the gyms row by id, excluding archived/dup rows.
  select count(*) into v_gym_count
    from public.gyms g
   where g.id = p_gym_id
     and lower(coalesce(g.slug, '')) not like '%archived%'
     and lower(coalesce(g.slug, '')) not like '%-dup%'
     and lower(coalesce(g.name, '')) not like '%archived%'
     and lower(coalesce(g.name, '')) not like '%do not use%';
  if v_gym_count is distinct from 1 then
    return;
  end if;

  -- The server-owned token mapping is the only account-key authority. Count
  -- all rows for this key, so duplicates and cross-tenant aliases fail closed.
  select count(*) into v_mapping_count
    from public.echo_intake_tokens t
    join public.content_calendar c on c.gym_id = t.echo_account_key
   where c.id = p_calendar_id and t.gym_id = p_gym_id;
  if v_mapping_count is distinct from 1 or
      (select count(*) from public.echo_intake_tokens t
        join public.content_calendar c on c.gym_id = t.echo_account_key
       where c.id = p_calendar_id) is distinct from 1 then
    return;
  end if;
  -- Hold the authoritative mapping through the stamp. The unique index above
  -- prevents another token row from taking this key while this row is locked.
  select t.gym_id into v_mapped_gym
    from public.echo_intake_tokens t
    join public.content_calendar c on c.gym_id = t.echo_account_key
   where c.id = p_calendar_id and t.gym_id = p_gym_id
   for share of t;
  if v_mapped_gym is distinct from p_gym_id then
    return;
  end if;

  -- Lock the exact row and require it to belong to THIS gym's canonical
  -- account key, be active/unpublished/approved.
  select * into v_row from public.content_calendar c
   where c.id = p_calendar_id
     and c.status = 'approved' and c.published_at is null
     and c.late_post_id is null and c.variant_status = 'active'
     and exists (select 1 from public.echo_intake_tokens t
                  where t.gym_id = p_gym_id
                    and t.echo_account_key = c.gym_id)
   for update of c;
  if not found then
    return;
  end if;

  -- Exact digest match: stored = recomputed from the locked row = Echo's.
  -- Only an unproved approval may be stamped. Concurrent portal taps must
  -- never replace the first authenticated approver's actor attribution.
  if v_row.approval_kind is not null or v_row.approved_by is not null
      or v_row.approved_at is not null
      or v_row.approval_digest is null
      or v_row.approval_digest is distinct from
         public.calendar_approval_digest(v_row)
      or v_row.approval_digest is distinct from v_digest then
    return;
  end if;

  return query
    update public.content_calendar c
       set approval_kind = 'human',
           approved_by = v_actor,
           approved_at = now()
     where c.id = p_calendar_id
    returning c.id, p_gym_id, v_actor, c.approval_digest;
end;
$$;

revoke all on function public.calendar_stamp_verified_approval(uuid, uuid, text, text)
  from public, anon, authenticated;
grant execute on function public.calendar_stamp_verified_approval(uuid, uuid, text, text)
  to service_role;

-- Resolve the gym's CURRENT autonomy inside the claim transaction. Returns
-- TRUE only when the key resolves through exactly one server-owned token
-- mapping to a clean gyms row (archived/dup rows never match), with EXACTLY
-- ONE echo_gym_settings row whose autonomous flag is true.
-- Zero rows, multiple candidate gyms, or multiple/missing settings rows are
-- AMBIGUOUS and return FALSE -- the caller fails closed onto the proof gate.
-- VOLATILE gives each SQL statement a current snapshot; FOR SHARE holds the
-- settings row through the caller's transaction, serializing mode updates.
create or replace function public.calendar_gym_is_autonomous(
  p_gym_id text
) returns boolean
language plpgsql
volatile
set search_path = public
as $$
declare
  v_gym_count integer;
  v_gym_uuid uuid;
  v_settings_count integer;
  v_autonomous boolean;
begin
  if nullif(btrim(coalesce(p_gym_id, '')), '') is null then
    return false;
  end if;
  select count(*), min(g.id::text)::uuid into v_gym_count, v_gym_uuid
    from public.gyms g
    join public.echo_intake_tokens t on t.gym_id = g.id
   where t.echo_account_key = p_gym_id
     and lower(coalesce(g.slug, '')) not like '%archived%'
     and lower(coalesce(g.slug, '')) not like '%-dup%'
     and lower(coalesce(g.name, '')) not like '%archived%'
     and lower(coalesce(g.name, '')) not like '%do not use%';
  if v_gym_count is distinct from 1 then
    return false;  -- unresolved or ambiguous: fail closed
  end if;
  -- Keep this exact key owned by the resolved gym through the claim. The
  -- unique key index prevents a competing insert/reassignment meanwhile.
  perform 1 from public.echo_intake_tokens t
   where t.gym_id = v_gym_uuid and t.echo_account_key = p_gym_id
   for share;
  if not found then
    return false;
  end if;
  -- Aggregation cannot lock rows. echo_gym_settings(gym_id) is the production
  -- primary key (verified 2026-10-05); lock that unique row before reading
  -- autonomy so a concurrent mode update serializes with this claim.
  select count(*) into v_settings_count from public.echo_gym_settings s
   where s.gym_id = v_gym_uuid;
  if v_settings_count is distinct from 1 then
    return false;  -- missing or duplicate settings: fail closed
  end if;
  select s.autonomous into v_autonomous from public.echo_gym_settings s
   where s.gym_id = v_gym_uuid for share;
  return coalesce(v_autonomous, false);
end;
$$;

revoke all on function public.calendar_gym_is_autonomous(text)
  from public, anon, authenticated;
grant execute on function public.calendar_gym_is_autonomous(text)
  to service_role;

-- Atomic claim with an OPTIONAL proof gate. p_require_approval_proof defaults
-- FALSE, preserving byte-for-byte current behavior for every caller. When TRUE
-- the claim re-reads the gym's CURRENT autonomy from the DB in this same
-- transaction: a definitively-autonomous gym keeps today's behavior exactly;
-- any other gym (Manual, or unresolved) must carry a fresh human proof whose
-- digest matches the LOCKED row's current fields -- a stale Automatic-era
-- approval or any post-approval edit of a publish-relevant field fails closed
-- here, atomically, in the same transaction as the claim.
drop function if exists public.claim_calendar_publish_slot_owned(
  uuid, text, date, text, integer, boolean);
create or replace function public.claim_calendar_publish_slot_owned(
  p_row_id uuid, p_gym_id text, p_day date, p_timezone text,
  p_capacity integer, p_approved_only boolean,
  p_require_approval_proof boolean default false
) returns uuid
language plpgsql security definer set search_path = public
as $$
declare
  v_row public.content_calendar%rowtype;
  v_used integer;
  v_token uuid;
  v_enforce_proof boolean;
begin
  if p_capacity < 1 or p_capacity > 3 or p_day is null or p_timezone is null
      or nullif(btrim(p_gym_id), '') is null then
    return null;
  end if;
  if p_capacity = 3 and p_gym_id <> 'lasso' then
    return null;
  end if;

  perform pg_advisory_xact_lock(hashtextextended(p_gym_id, 0));

  -- ATOMIC MODE CHECK (armed only when the caller passes TRUE, which Echo does
  -- for every lane behind AGENT_APPROVAL_PROOF): decide Manual vs Autonomous
  -- from the authoritative DB NOW, inside the claim's own transaction and
  -- with a settings-row lock -- never from a stale publisher snapshot. A
  -- completed Auto->Manual flip therefore takes effect at the next claim. Any
  -- resolver ambiguity enforces the proof gate (fail closed).
  v_enforce_proof := p_require_approval_proof
                     and not public.calendar_gym_is_autonomous(p_gym_id);

  select * into v_row from public.content_calendar
    where id = p_row_id and gym_id = p_gym_id
      and status in ('pending', 'approved') and published_at is null
      and late_post_id is null and variant_status = 'active'
      -- media hold guards: never claim a row without real, ready media
      and nullif(btrim(coalesce(image_url, '')), '') is not null
      and media_not_ready_reason is null
    for update;
  if not found or (p_approved_only and v_row.status <> 'approved') then
    return null;
  end if;
  -- A gym the DB says is Manual right now may only publish APPROVED rows,
  -- even if the worker's stale snapshot ran the autonomous lane.
  if v_enforce_proof and v_row.status <> 'approved' then
    return null;
  end if;
  if p_capacity = 3 and
      coalesce(nullif(lower(btrim(v_row.format)), ''), 'feed') <> 'feed' then
    return null;
  end if;

  -- APPROVAL PROOF GATE: a fresh VERIFIED human approval whose digest matches
  -- the LOCKED row. Review defect 1: approval_kind='human' can only be stamped
  -- by calendar_stamp_verified_approval, which requires a nonempty Clerk actor
  -- -- an Echo bearer token alone (approval_kind NULL) NEVER satisfies this
  -- gate. approved_by must be NONEMPTY: proof without a trusted actor is not
  -- proof. The digest binds the FINAL image_url, so changed pixels (auto-fit,
  -- reburn, swap) after the stamp fail closed here.
  if v_enforce_proof then
    if v_row.approval_kind is distinct from 'human'
        or nullif(btrim(coalesce(v_row.approved_by, '')), '') is null
        or v_row.approved_at is null
        or v_row.approval_digest is null
        or v_row.approval_digest is distinct from
           public.calendar_approval_digest(v_row) then
      return null;
    end if;
  end if;

  select count(*) into v_used from public.content_calendar
    where gym_id = p_gym_id
      and lower(btrim(coalesce(account, ''))) =
          lower(btrim(coalesce(v_row.account, '')))
      and coalesce(nullif(lower(btrim(format)), ''), 'feed') =
          coalesce(nullif(lower(btrim(v_row.format)), ''), 'feed')
      and ((status = 'publishing' and publish_reservation_day = p_day)
           or (status = 'published' and
               (publish_reservation_day = p_day
                or (published_at is not null and
                    (published_at at time zone p_timezone)::date = p_day))));
  if v_used >= p_capacity then
    return null;
  end if;

  v_token := gen_random_uuid();
  update public.content_calendar
    set status = 'publishing', publish_reservation_day = p_day,
        publish_claim_token = v_token
    where id = p_row_id;
  return v_token;
end;
$$;

revoke all on function public.claim_calendar_publish_slot_owned(
  uuid, text, date, text, integer, boolean, boolean)
  from public, anon, authenticated;
grant execute on function public.claim_calendar_publish_slot_owned(
  uuid, text, date, text, integer, boolean, boolean)
  to service_role;
