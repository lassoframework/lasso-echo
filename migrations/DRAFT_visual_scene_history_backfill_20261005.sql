-- DRAFT / UNAPPLIED / OFF — DO NOT APPLY, DO NOT ACTIVATE.
--
-- STATUS: DRAFT / UNAPPLIED / OFF — fail-closed historical occupied-scene
-- coverage report and published-row backfill (Child A, Echo history
-- milestone, 2026-10-05). This file adds TWO things on top of the scene
-- ledger draft and changes nothing else:
--   (1) public.visual_scene_history_coverage() — a READ-ONLY report over
--       every published historical content_calendar row classifying it as
--       'covered' (occupancy already recorded), 'backfillable' (every
--       evidence check below passes), or 'blocked' with an EXPLICIT reason.
--       Unresolved rows are never silently skipped, never fabricated, and
--       never marked cleared: they are named blockers.
--   (2) public.visual_scene_backfill_occupied() — replaces the 0A000 stub
--       from DRAFT_visual_scene_ledger_20261005.sql (same signature, same
--       owner-only privilege posture) with a guarded writer that records
--       occupancy ONLY for rows the coverage report classifies
--       'backfillable'. It writes NOTHING for blocked rows, never touches
--       candidates, never writes holds, never arms any guard, never flips
--       any flag, and refuses to run once any tenant is armed.
--
-- A row is 'backfillable' IF AND ONLY IF ALL of the following hold:
--   * canonical tenant: visual_group_tenant_id(row.gym_id) resolves
--     (unmapped calendar keys are blockers, never minted);
--   * canonical group: visual_group_resolve_row(row) resolves from the row's
--     current delivered object and aliases (caller-persisted keys are hints,
--     never authority);
--   * canonical date: row.post_date IS NOT NULL (undated published history
--     is a blocker, mirroring the exact-byte ledger's NULL-date semantics);
--   * displayed object: visual_scene_row_delivered_object(row) yields the
--     row's exact DELIVERED object (display image, or poster when the video
--     thumbnail is distinct);
--   * owner pHash receipt: EXACTLY ONE visual_scene_owner_phash_receipt
--     binds (canonical tenant, resolved group, object role, exact URL) and
--     its fingerprint/byte_length/algorithm match the immutable
--     visual_global_object_attestation for that object (zero receipts =
--     'no_owner_phash_receipt'; more than one = 'ambiguous_owner_receipt');
--   * verified source/delivery lineage:
--     visual_global_row_bytes_verified_for(row, resolved_group) is true —
--     selected source bytes, delivered bytes and any distinct poster are all
--     attested scene members with owner render lineage.
-- Any failure leaves the row an explicit blocker. No evidence is invented.
--
-- MIGRATION ORDER (exact):
--   1. migrations/DRAFT_visual_group_schema_20261002.sql
--   2. migrations/DRAFT_visual_group_claim_trigger_20261002.sql
--   3. migrations/DRAFT_visual_global_history_20261002.sql
--   4. migrations/DRAFT_visual_group_backfill_20261002.sql
--   5. migrations/DRAFT_visual_group_activation_20261002.sql
--   6. migrations/DRAFT_visual_scene_ledger_20261005.sql
--   7. THIS FILE: migrations/DRAFT_visual_scene_history_backfill_20261005.sql
--   8. (any activation draft — must remain LAST; none exists yet)
--
-- ROLLBACK (before any activation): nothing is applied anywhere; delete the
-- file. If it were ever applied to a scratch database, restore the stub and
-- drop the report:
--   drop function if exists public.visual_scene_history_coverage();
--   create or replace function public.visual_scene_backfill_occupied()
--   returns integer language plpgsql security definer set search_path = public as $$
--   begin
--     raise exception 'visual_scene_backfill_occupied is an activation-draft deliverable; occupied history must be derived from the exact-byte ledger before activation (0A000 stub)'
--       using errcode='0A000';
--   end; $$;
--   revoke all on function public.visual_scene_backfill_occupied()
--     from public, anon, authenticated, service_role;
-- Backfilled occupancy rows are the same append-only evidence the claim path
-- writes; preserve them (the occupied table's immutability trigger already
-- refuses UPDATE/DELETE).
--
-- FAIL-CLOSED GUARANTEES:
--   * The backfill RAISES (0A000) if any tenant is armed in
--     gym_visual_guard_settings: it is a pre-activation import only and must
--     never run against a live guarded fleet.
--   * Requires READ COMMITTED isolation (25001 otherwise), matching the
--     exact-byte ledger's alias guard.
--   * Takes the single fleet-wide scene advisory lock before writing, so it
--     serializes with any claim-path scan.
--   * Idempotent: re-recording an identical occupied row conflicts on the
--     occupied PK and inserts nothing; returns the count actually inserted
--     by THIS call.
--   * Exact-receipt binding: coverage binds the ONE owner pHash receipt
--     validated by the attestation join and exposes its receipt_id; the
--     backfill writes from that receipt_id only — a mismatched second
--     receipt on the same tenant/group/role/URL cannot contaminate
--     phash/fingerprint/evidence.
--   * TOCTOU guard: the backfill locks each candidate calendar row FOR
--     UPDATE and re-classifies the CURRENT row under the lock immediately
--     before insertion; a stale or changed classification writes nothing.
--   * Records ONLY similarity evidence derived from immutable owner receipts
--     and verified lineage. It never writes visual_group_scene_link (human-
--     confirmed only), never resolves holds, never mutates candidates,
--     never arms any guard and never flips any flag.
--
-- This file resolves ONLY backfill blocker 4's derivation; it does not arm
-- anything and no acceptance run has occurred.
begin;

-- ---------------------------------------------------------------------------
-- (1) Coverage report. READ-ONLY classification of every published historical
-- calendar row. Published means status='published' OR published_at IS NOT
-- NULL (mirrors the claim trigger's finalized test). Every published row is
-- classified exactly once; nothing is filtered out. state values:
--   'covered'      occupancy already recorded for the row's resolved
--                  (phash, canonical tenant, group, date);
--   'backfillable' all evidence checks pass; occupancy not yet recorded;
--   'blocked'      explicit blocker_reason:
--                    unmapped_tenant | unresolved_group | missing_date |
--                    no_delivered_object | no_owner_phash_receipt |
--                    ambiguous_owner_receipt | lineage_unverified
-- SECURITY DEFINER so service_role review tooling can read owner receipts
-- (that table is revoked from all application roles by design). It writes
-- nothing and never raises for a row state.
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_history_coverage()
returns table(calendar_row_id uuid, tenant_id text, group_key text,
              post_date date, object_role text, exact_url text,
              phash char(16), owner_receipt_id uuid,
              state text, blocker_reason text)
language plpgsql stable security definer set search_path = public as $$
declare v_row public.content_calendar%rowtype;
  v_tenant text; v_group text; v_obj record;
  v_receipts bigint; v_receipt public.visual_scene_owner_phash_receipt%rowtype;
begin
  for v_row in
    select c.* from public.content_calendar c
      where c.status = 'published' or c.published_at is not null
      order by c.post_date nulls last, c.id
  loop
    calendar_row_id := v_row.id;
    tenant_id := null; group_key := null; post_date := v_row.post_date;
    object_role := null; exact_url := null; phash := null;
    owner_receipt_id := null;
    state := 'blocked'; blocker_reason := null;
    -- Canonical tenant: unmapped calendar keys are blockers, never minted.
    v_tenant := public.visual_group_tenant_id(v_row.gym_id)::text;
    if v_tenant is null then
      blocker_reason := 'unmapped_tenant'; return next; continue;
    end if;
    tenant_id := v_tenant;
    -- Canonical group: re-resolved from the row's current delivered object
    -- and aliases; the persisted visual_group_key is a hint, not authority.
    v_group := public.visual_group_resolve_row(v_row);
    if nullif(btrim(v_group), '') is null then
      blocker_reason := 'unresolved_group'; return next; continue;
    end if;
    group_key := v_group;
    if v_row.post_date is null then
      blocker_reason := 'missing_date'; return next; continue;
    end if;
    -- The row's exact DELIVERED object (display image, or distinct poster).
    select d.object_role, d.exact_url into v_obj
      from public.visual_scene_row_delivered_object(v_row) d;
    if v_obj.exact_url is null then
      blocker_reason := 'no_delivered_object'; return next; continue;
    end if;
    object_role := v_obj.object_role; exact_url := v_obj.exact_url;
    -- Owner pHash receipt: EXACTLY ONE immutable owner computation bound to
    -- this canonical tenant/group/role/URL AND to the attested exact bytes.
    select count(*) into v_receipts
      from public.visual_scene_owner_phash_receipt r
      join public.visual_global_object_attestation o
        on o.tenant_id = r.tenant_id and o.group_key = r.group_key
        and o.exact_url = r.exact_url and o.fingerprint = r.fingerprint
        and o.byte_length = r.byte_length
      where r.tenant_id = v_tenant and r.group_key = v_group
        and r.object_role = v_obj.object_role and r.exact_url = v_obj.exact_url
        and r.algorithm = 'echo-dct-phash64-v1';
    if coalesce(v_receipts, 0) = 0 then
      blocker_reason := 'no_owner_phash_receipt'; return next; continue;
    end if;
    if v_receipts > 1 then
      blocker_reason := 'ambiguous_owner_receipt'; return next; continue;
    end if;
    -- Bind the EXACT validated receipt through the SAME attestation join as
    -- the count: a mismatched second receipt on the same binding keys is
    -- never selected, so it cannot contaminate phash/fingerprint/evidence.
    select r.* into v_receipt
      from public.visual_scene_owner_phash_receipt r
      join public.visual_global_object_attestation o
        on o.tenant_id = r.tenant_id and o.group_key = r.group_key
        and o.exact_url = r.exact_url and o.fingerprint = r.fingerprint
        and o.byte_length = r.byte_length
      where r.tenant_id = v_tenant and r.group_key = v_group
        and r.object_role = v_obj.object_role and r.exact_url = v_obj.exact_url
        and r.algorithm = 'echo-dct-phash64-v1'
      limit 1;
    phash := v_receipt.phash;
    owner_receipt_id := v_receipt.receipt_id;
    -- Verified source/delivery lineage against the RESOLVED group.
    if not public.visual_global_row_bytes_verified_for(v_row, v_group) then
      blocker_reason := 'lineage_unverified'; return next; continue;
    end if;
    if exists(select 1 from public.visual_scene_phash_occupied o
        where o.phash = v_receipt.phash and o.tenant_id = v_tenant
          and o.group_key = v_group and o.used_date = v_row.post_date) then
      state := 'covered';
    else
      state := 'backfillable';
    end if;
    return next;
  end loop;
  return;
end;
$$;

-- ---------------------------------------------------------------------------
-- (2) Guarded backfill. Replaces the 0A000 stub (same signature and return
-- type). Records occupancy ONLY for rows the coverage report classifies
-- 'backfillable' — every evidence check above has already passed for them.
-- Blocked rows are untouched and remain named blockers in the report.
-- Returns the number of occupied rows actually inserted by THIS call.
-- Owner-only; revoked from every application role below.
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_backfill_occupied()
returns integer language plpgsql security definer set search_path = public as $$
declare v_row record; v_now record; v_inserted integer := 0; v_hit bigint;
  v_locked public.content_calendar%rowtype;
begin
  -- Pre-activation import only: refuse once ANY tenant is armed.
  if exists(select 1 from public.gym_visual_guard_settings where enforce) then
    raise exception 'visual_scene_backfill_occupied is pre-activation only; an armed tenant exists (0A000)'
      using errcode='0A000';
  end if;
  if current_setting('transaction_isolation') <> 'read committed' then
    raise exception 'visual scene history backfill requires READ COMMITTED isolation'
      using errcode='25001';
  end if;
  -- Serialize with the claim path: the single fleet-wide scene advisory lock.
  if not pg_try_advisory_xact_lock(hashtextextended(
      jsonb_build_array('visual_scene_global')::text, 0)) then
    raise exception 'global scene claim busy; retry transaction' using errcode='55P03';
  end if;
  for v_row in
    select * from public.visual_scene_history_coverage() c
      where c.state = 'backfillable'
      order by c.post_date, c.calendar_row_id
  loop
    -- TOCTOU guard: lock the CURRENT calendar row, then re-classify it
    -- under the lock. The candidate classification above was read before
    -- the row lock existed and is stale by definition; only a fresh,
    -- identical classification of the locked row permits a write.
    select c.* into v_locked from public.content_calendar c
      where c.id = v_row.calendar_row_id for update;
    if not found then
      continue;
    end if;
    select c.* into v_now from public.visual_scene_history_coverage() c
      where c.calendar_row_id = v_row.calendar_row_id;
    if not found
      or v_now.state <> 'backfillable'
      or v_now.owner_receipt_id is distinct from v_row.owner_receipt_id
      or v_now.phash is distinct from v_row.phash
      or v_now.post_date is distinct from v_row.post_date
      or v_now.exact_url is distinct from v_row.exact_url
      or v_now.object_role is distinct from v_row.object_role then
      continue;  -- row changed or no longer fully evidenced: NO write.
    end if;
    -- Insert from the EXACT validated receipt bound through coverage — no
    -- re-lookup by tenant/group/role/URL, so a mismatched second receipt
    -- can never contaminate phash/fingerprint/evidence.
    insert into public.visual_scene_phash_occupied
      (phash, tenant_id, group_key, used_date, fingerprint,
       calendar_row_id, channel, evidence)
    select v_now.phash, v_now.tenant_id, v_now.group_key, v_now.post_date,
      r.fingerprint, v_now.calendar_row_id, v_locked.account,
      jsonb_build_object(
        'source', 'historical_backfill_draft_20261005',
        'owner_phash_receipt', r.receipt_id,
        'exact_url', v_now.exact_url,
        'object_role', v_now.object_role)
      from public.visual_scene_owner_phash_receipt r
      where r.receipt_id = v_now.owner_receipt_id
    on conflict (phash, tenant_id, group_key, used_date) do nothing;
    get diagnostics v_hit = row_count;
    v_inserted := v_inserted + v_hit;
  end loop;
  return v_inserted;
end;
$$;

-- ---------------------------------------------------------------------------
-- Privileges. The coverage report is a safe read surface: revoke from
-- public/anon/authenticated, grant to service_role for review tooling. The
-- backfill stays owner-only (same posture as the 0A000 stub it replaces).
-- ---------------------------------------------------------------------------
revoke all on function public.visual_scene_history_coverage()
  from public, anon, authenticated, service_role;
grant execute on function public.visual_scene_history_coverage() to service_role;
revoke all on function public.visual_scene_backfill_occupied()
  from public, anon, authenticated, service_role;

commit;
