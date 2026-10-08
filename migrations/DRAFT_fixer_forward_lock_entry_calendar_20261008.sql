-- DRAFT / UNAPPLIED / DEFAULT OFF. No production activation is authorized.
-- Forward lock entry, calendar tranche (child 0): the six production calendar
-- functions below acquire the ordinary entry sequence FIRST -- the graph shared
-- lock 'G' (pg_advisory_xact_lock_shared on 'fixer_forward_graph_20261006'),
-- then the census-exclusive transaction entry lock 'C' (pg_advisory_xact_lock
-- on 'fixer_forward_photo_census_20261007') -- before ANY existing advisory or
-- row lock they already take. This matches the entry order established by
-- DRAFT_fixer_forward_media_claim_20261006.sql (claim/binder) and
-- DRAFT_fixer_owner_photo_clearance_20261007.sql (corpus write lock): no caller
-- may hold a row lock or tenant advisory lock while waiting on G or C, so a
-- graph/census wait can never invert the lock order. Runtime publishers never
-- upgrade the shared graph lock; attestation graph mutation is the only
-- exclusive graph holder and it waits without holding calendar rows.
--
-- Both advisory keys are the EXISTING system-wide keys, not new tranche-local
-- keys: the ordering only protects if every participant serializes on the same
-- lock namespace.
--
-- Helper (least privilege): public.fixer_forward_calendar_entry_lock_20261008()
--   returns void, language plpgsql, security definer,
--   set search_path=pg_catalog,public. It raises (errcode 25000) unless the
--   transaction isolation is read committed, then takes G (shared) then C
--   (exclusive), in that order. EXECUTE is revoked from
--   public/anon/authenticated/service_role: the replaced calendar functions
--   are SECURITY DEFINER and reach it as their owner; no direct caller role is
--   granted. Child 1 should CALL this helper for the same entry sequence.
--
-- Function text: each CREATE OR REPLACE below is the exact frozen production
-- definition from evidence/portal-function-definitions-20261008.sql with ONLY
-- the entry-lock call inserted at the top of the body (and, for
-- approve_calendar_row_if_media_ready, a LANGUAGE sql -> plpgsql wrapper so
-- the lock can precede its single UPDATE; the UPDATE statement, its embedded
-- contract comment, predicate, return contract and SECURITY DEFINER setting
-- are preserved verbatim). Signatures, defaults, tenant/idempotency/approval
-- checks and capacity rules are unchanged. No provider (external API) call
-- exists in these bodies; nothing is added outside DB lock scope.
--
-- Drift precondition: the DO block below aborts the whole migration unless
-- every target function still exists exactly once in public with
-- exact identity arguments from the frozen CREATE signature and read-only
-- evidence/portal-entry-function-identities-20261008.json, owner postgres,
-- current_user postgres, and md5(prosrc) equal to the frozen inventory hash
-- (evidence/portal-legacy-function-inventory-20261008.json, query:
-- md5(p.prosrc)). Any production drift -- body edit, drop, or a duplicate
-- overload -- raises 23514 and rolls the transaction back before any replace.
--
-- Rollback before use: restore the frozen definitions from the evidence file
-- and drop public.fixer_forward_calendar_entry_lock_20261008().
begin;

-- PRECONDITION GUARD: frozen production definitions (abort on any drift).
do $$
declare
  v_name text;
  v_expected text;
  v_expected_identity text;
  v_identity text;
  v_owner text;
  v_count integer;
  v_actual text;
begin
  if current_user is distinct from 'postgres' then
    raise exception 'forward lock entry DRAFT: current_user must be postgres' using errcode = '23514';
  end if;
  for v_name, v_expected, v_expected_identity in
    select f.proname, f.body_md5, f.identity_args from (values
      ('approve_calendar_row_if_media_ready', '1670c2099e1fa3cea0df22c786980790', 'p_row_id uuid, p_gym_id text, p_expected jsonb'),
      ('calendar_recover_unproved_approval', '7c509400fe0322d87f8c39b2d0254edd', 'p_row_id uuid, p_gym_id text, p_expected jsonb'),
      ('calendar_stamp_verified_approval', '704fb687d1126d6904dd4cfcaa6fe2ce', 'p_gym_id uuid, p_calendar_id uuid, p_clerk_actor_id text, p_echo_approval_digest text'),
      ('claim_calendar_gbp_publish_owned', '5dc70ca074016626f32af9772696b241', 'p_row_id uuid, p_gym_id text'),
      ('claim_calendar_publish_slot_owned', 'db59bf4d6d0be4c42e5e49ab0b5a8b4f', 'p_row_id uuid, p_gym_id text, p_day date, p_timezone text, p_capacity integer, p_approved_only boolean, p_require_approval_proof boolean'),
      ('content_calendar_swap_variant', '548886200ea1a64ad6147f7504ec4e00', 'p_gym_id text, p_candidate_id uuid, p_actor text')
    ) as f(proname, body_md5, identity_args)
  loop
    select count(*), max(md5(p.prosrc)),
           max(pg_get_function_identity_arguments(p.oid)), max(pg_get_userbyid(p.proowner))
      into v_count, v_actual, v_identity, v_owner
      from pg_proc p join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public' and p.proname = v_name;
    if v_count is distinct from 1 or v_actual is distinct from v_expected
        or v_identity is distinct from v_expected_identity
        or v_owner is distinct from 'postgres' then
      raise exception 'forward lock entry DRAFT: production definition of % drifted from the frozen 2026-10-08 inventory (count %, md5 %, identity %, owner %); aborting before any replace',
        v_name, v_count, v_actual, v_identity, v_owner using errcode = '23514';
    end if;
  end loop;
end $$;

-- ORDINARY ENTRY SEQUENCE, single least-privilege helper. G (shared graph)
-- first, then C (exclusive census entry). Never upgrade G in a runtime lane.
create function public.fixer_forward_calendar_entry_lock_20261008()
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
begin
  -- Read Committed refreshes statement snapshots after advisory-lock waits;
  -- a repeatable snapshot could miss the census/edge that just committed.
  if current_setting('transaction_isolation') <> 'read committed' then
    raise exception 'forward calendar entry lock requires read committed isolation' using errcode = '25000';
  end if;
  perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006', 0));
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007', 0));
end;
$$;
-- Least privilege: only SECURITY DEFINER owners reach it; no runtime role may
-- invoke the entry lock directly or compose a different order around it.
revoke all on function public.fixer_forward_calendar_entry_lock_20261008()
  from public, anon, authenticated, service_role;

CREATE OR REPLACE FUNCTION public.approve_calendar_row_if_media_ready(p_row_id uuid, p_gym_id text, p_expected jsonb DEFAULT NULL::jsonb)
 RETURNS SETOF content_calendar
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
begin
  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census
  -- exclusive 'C', before the UPDATE below takes any row lock.
  perform public.fixer_forward_calendar_entry_lock_20261008();
  return query
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
         -- account platform. GBP also compares every raw digest-bound field.
         -- ALL of it is checked in THIS SAME UPDATE that
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
         and (lower(btrim(c.account)) <> 'googlebusiness'
              or public.calendar_gbp_approval_snapshot(c) = p_expected->'gbp_proof')
       )
     )
  returning *;
end;
$function$
;

CREATE OR REPLACE FUNCTION public.calendar_recover_unproved_approval(p_row_id uuid, p_gym_id text, p_expected jsonb)
 RETURNS SETOF content_calendar
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_row public.content_calendar%rowtype;
begin
  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census
  -- exclusive 'C', before any row lock (the FOR UPDATE below).
  perform public.fixer_forward_calendar_entry_lock_20261008();
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
      or (v_row.approval_digest is not null and
          v_row.approval_digest is distinct from public.calendar_approval_digest(v_row))
      or v_row.caption is distinct from p_expected->>'caption'
      or v_row.image_url is distinct from p_expected->>'media_url'
      or v_row.post_date::text is distinct from p_expected->>'day_key'
      or coalesce(nullif(lower(btrim(v_row.format)), ''), 'feed')
         is distinct from coalesce(nullif(lower(btrim(coalesce(p_expected->>'format', ''))), ''), 'feed')
      or coalesce(nullif(lower(btrim(v_row.account)), ''), '')
         is distinct from lower(btrim(coalesce(p_expected->>'platform', '')))
      or (lower(btrim(v_row.account)) = 'googlebusiness' and
          public.calendar_gbp_approval_snapshot(v_row)
            is distinct from p_expected->'gbp_proof') then
    return;
  end if;
  if v_row.approval_digest is null then
    return query
      update public.content_calendar c
         set approval_digest = public.calendar_approval_digest(c)
       where c.id = p_row_id
      returning c.*;
  else
    return next v_row;
  end if;
end;
$function$
;

CREATE OR REPLACE FUNCTION public.calendar_stamp_verified_approval(p_gym_id uuid, p_calendar_id uuid, p_clerk_actor_id text, p_echo_approval_digest text)
 RETURNS TABLE(id uuid, gym_id uuid, clerk_actor_id text, approval_digest text)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_actor text := nullif(btrim(coalesce(p_clerk_actor_id, '')), '');
  v_digest text := nullif(btrim(coalesce(p_echo_approval_digest, '')), '');
  v_gym_count integer;
  v_mapping_count integer;
  v_mapped_gym uuid;
  v_row public.content_calendar%rowtype;
begin
  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census
  -- exclusive 'C', before the FOR SHARE mapping lock and the FOR UPDATE row
  -- lock below.
  perform public.fixer_forward_calendar_entry_lock_20261008();
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
$function$
;

CREATE OR REPLACE FUNCTION public.claim_calendar_gbp_publish_owned(p_row_id uuid, p_gym_id text)
 RETURNS SETOF content_calendar
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_row public.content_calendar%rowtype;
  v_enforce_proof boolean;
begin
  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census
  -- exclusive 'C', before the tenant advisory lock and the FOR UPDATE below.
  perform public.fixer_forward_calendar_entry_lock_20261008();
  if p_row_id is null or nullif(btrim(coalesce(p_gym_id, '')), '') is null then
    return;
  end if;
  perform pg_advisory_xact_lock(hashtextextended(p_gym_id, 0));
  v_enforce_proof := not public.calendar_gym_is_autonomous(p_gym_id);
  select * into v_row from public.content_calendar c
    where c.id = p_row_id and c.gym_id = p_gym_id
      and c.account = 'googlebusiness' and c.status = 'approved'
      and c.variant_status = 'active' and c.published_at is null
      and c.late_post_id is null and c.publish_claim_token is null
      and c.media_not_ready_reason is null
      and nullif(btrim(coalesce(c.image_url, '')), '') is not null
    for update;
  if not found then return; end if;
  if v_enforce_proof and (
      v_row.approval_kind is distinct from 'human'
      or nullif(btrim(coalesce(v_row.approved_by, '')), '') is null
      or v_row.approved_at is null or v_row.approval_digest is null
      or v_row.approval_digest is distinct from
         public.calendar_approval_digest(v_row)) then
    return;
  end if;
  return query update public.content_calendar c
    set status = 'publishing', publish_claim_token = gen_random_uuid()
    where c.id = p_row_id returning c.*;
end;
$function$
;

CREATE OR REPLACE FUNCTION public.claim_calendar_publish_slot_owned(p_row_id uuid, p_gym_id text, p_day date, p_timezone text, p_capacity integer, p_approved_only boolean, p_require_approval_proof boolean DEFAULT false)
 RETURNS uuid
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_row public.content_calendar%rowtype;
  v_used integer;
  v_current_used integer;
  v_backlog_used integer;
  v_token uuid;
  v_enforce_proof boolean;
begin
  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census
  -- exclusive 'C', before the tenant advisory lock and any row lock below.
  perform public.fixer_forward_calendar_entry_lock_20261008();
  if p_capacity is null or p_approved_only is null
      or p_require_approval_proof is null
      or p_capacity < 1 or (p_capacity > 3 and p_capacity not in (5, 15))
      or p_day is null or p_timezone is null
      or nullif(btrim(p_gym_id), '') is null then
    return null;
  end if;
  if p_capacity = 3 and p_gym_id <> 'lasso' then
    return null;
  end if;
  if p_capacity = 5 and not (
      p_gym_id = 'lasso'
      and p_day between date '2026-10-07' and date '2026-10-11'
      and p_timezone = 'America/New_York') then
    return null;
  end if;
  if p_capacity = 15 and not (
      p_gym_id = 'lasso'
      and p_day between date '2026-10-05' and date '2026-10-06'
      and p_timezone = 'America/New_York') then
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
  -- Never reclaim a pending/approved row with an unresolved claim token or
  -- reservation day. This guard applies at every capacity and proof setting.
  if v_row.publish_claim_token is not null
      or v_row.publish_reservation_day is not null then
    return null;
  end if;
  -- A gym the DB says is Manual right now may only publish APPROVED rows,
  -- even if the worker's stale snapshot ran the autonomous lane.
  if v_enforce_proof and v_row.status <> 'approved' then
    return null;
  end if;
  if p_capacity in (5, 15) and
      (v_row.post_date is null
       or nullif(btrim(coalesce(v_row.account, '')), '') is null) then
    return null;
  end if;
  if p_capacity in (3, 5, 15) and
      coalesce(nullif(lower(btrim(v_row.format)), ''), 'feed') not in ('feed', 'story') then
    return null;
  end if;
  if p_capacity = 5 and not (
      v_row.post_date = p_day
      or v_row.post_date between date '2026-10-02' and date '2026-10-05') then
    return null;
  end if;
  if p_capacity = 15 and not (
      v_row.post_date = p_day
      or (v_row.post_date between date '2026-10-02' and date '2026-10-05'
          and v_row.post_date < p_day)) then
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

  if p_capacity = 5 then
    -- Three current-day slots plus two outage slots on October 7-11.
    -- The tenant advisory lock serializes both class counts.
    select count(*) filter (where post_date = p_day),
           count(*) filter (where post_date between date '2026-10-02'
                                            and date '2026-10-05')
      into v_current_used, v_backlog_used
      from public.content_calendar
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
    if (v_row.post_date = p_day and v_current_used >= 3)
        or (v_row.post_date <> p_day and v_backlog_used >= 2) then
      return null;
    end if;
  end if;

  if p_capacity = 15 then
    -- Three current-day slots plus twelve older-backlog slots on October 5-6.
    -- A post dated October 5 is current on the fifth, backlog on the sixth.
    select count(*) filter (where post_date = p_day),
           count(*) filter (where post_date between date '2026-10-02'
                                            and date '2026-10-05'
                                and post_date < p_day)
      into v_current_used, v_backlog_used
      from public.content_calendar
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
    if (v_row.post_date = p_day and v_current_used >= 3)
        or (v_row.post_date < p_day and v_backlog_used >= 12) then
      return null;
    end if;
  end if;

  v_token := gen_random_uuid();
  update public.content_calendar
    set status = 'publishing', publish_reservation_day = p_day,
        publish_claim_token = v_token
    where id = p_row_id;
  return v_token;
end;
$function$
;

CREATE OR REPLACE FUNCTION public.content_calendar_swap_variant(p_gym_id text, p_candidate_id uuid, p_actor text DEFAULT NULL::text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_candidate   public.content_calendar%rowtype;
  v_anchor      uuid;
  v_active      public.content_calendar%rowtype;
  v_active_found boolean := false;
begin
  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census
  -- exclusive 'C', before the candidate/group FOR UPDATE row locks below.
  perform public.fixer_forward_calendar_entry_lock_20261008();
  -- Lock the candidate row first, scoped by gym. A missing row or a
  -- cross-gym id both come back as the SAME 'not_found' -- never reveals
  -- that a row exists under a different tenant.
  select * into v_candidate
    from public.content_calendar
   where id = p_candidate_id
     and gym_id = p_gym_id
   for update;

  if not found then
    return jsonb_build_object('ok', false, 'error', 'not_found');
  end if;

  if v_candidate.variant_status <> 'candidate' then
    return jsonb_build_object('ok', false, 'error', 'not_a_candidate',
      'variant_status', v_candidate.variant_status);
  end if;

  if v_candidate.status = 'published' then
    return jsonb_build_object('ok', false, 'error', 'published_final');
  end if;

  v_anchor := coalesce(v_candidate.variant_of, v_candidate.id);

  -- Lock the WHOLE group (anchor row itself, plus every row whose variant_of
  -- points at the anchor), gym-scoped, so nothing in it can change under us
  -- for the rest of this transaction.
  perform 1
    from public.content_calendar
   where gym_id = p_gym_id
     and (id = v_anchor or variant_of = v_anchor)
   for update;

  -- Find the group's current active row (there is at most one, by the
  -- unique index; there may be zero only if the group was left in a bad
  -- state by something outside this function, which we treat as "no prior
  -- active to archive" rather than erroring).
  select * into v_active
    from public.content_calendar
   where gym_id = p_gym_id
     and (id = v_anchor or variant_of = v_anchor)
     and variant_status = 'active'
   limit 1;
  v_active_found := found;

  if v_active_found and v_active.status = 'published' then
    return jsonb_build_object('ok', false, 'error', 'published_final');
  end if;

  if v_active_found and v_active.id = v_candidate.id then
    -- Nothing to do; already active (should be unreachable given the
    -- variant_status='candidate' check above, kept as a defensive no-op).
    return jsonb_build_object('ok', true, 'noop', true,
      'active_id', v_active.id::text);
  end if;

  -- Archive every OTHER candidate in the group first (including a stale
  -- 'active' left dangling from a bad prior state), THEN promote the pick.
  -- Order matters for the unique index: archive-before-activate never has
  -- two rows both 'active' at once, even momentarily within this statement
  -- sequence (each UPDATE is its own statement, checked immediately).
  update public.content_calendar
     set variant_status = 'archived'
   where gym_id = p_gym_id
     and (id = v_anchor or variant_of = v_anchor)
     and id <> v_candidate.id
     and variant_status in ('active', 'candidate');

  update public.content_calendar
     set variant_status = 'active'
   where id = v_candidate.id
     and gym_id = p_gym_id;

  return jsonb_build_object(
    'ok', true,
    'active_id', v_candidate.id::text,
    'archived_previous_active', case when v_active_found
                                      then v_active.id::text else null end,
    'group_anchor', v_anchor::text
  );
end;
$function$
;

commit;
