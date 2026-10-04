-- DRAFT / UNAPPLIED / OFF — DO NOT APPLY, DO NOT ACTIVATE.
--
-- STATUS: DRAFT / UNAPPLIED / OFF — claim-wave redesign, 2026-10-03;
-- P0 repair pass (Sol/Astra SCENE_AUDIT_GAPS.md), 2026-10-04;
-- scene guard repair WAVE 2 (Astra SCENE_REPAIR_WAVE2_SCOPE.md items 1-4,
-- 2026-10-04);
-- scene guard repair WAVE 3 (Astra SCENE_REPAIR_WAVE3_SCOPE.md items 1-3,
-- overriding the wave-2 self-report per Sol SCENE_AUDIT_GAPS_WAVE2.md,
-- 2026-10-04). Still OFF: no
-- production apply, no flag activation. The scene decision no longer installs
-- its own calendar trigger: it lives INSIDE the exact-byte guard
-- (visual_group_guard_trigger, DRAFT_visual_group_claim_trigger_20261002.sql),
-- the ONE authoritative content_calendar BEFORE path — see WAVE-3 REPAIRS
-- below. SCENE_GUARD_OPERATIONAL stays False and
-- AGENT_VISUAL_SCENE_GUARD stays OFF.
--
-- UPGRADE/REPLAY IDEMPOTENCY (Sol frozen-hash audit, P0, 2026-10-04): a
-- database that previously applied the WAVE-2 revision of this draft still
-- carries the separate early scene trigger
-- content_calendar_scene_wave_claim_guard and its function
-- public.visual_scene_calendar_claim_guard() (a zero-argument trigger
-- function). Wave 3 removed their CREATION but must also DROP the leftovers:
-- a replay/upgrade that skips this leaves the old separate early trigger
-- live next to the merged guard — the exact durable-hold failure wave 3
-- removed (a later raising trigger can roll back the earlier trigger's
-- holds). The DROP ... IF EXISTS statements immediately after `begin;` below
-- remove those wave-2 leftovers. On a clean bootstrap they are harmless
-- no-ops (NOTICE only); on an upgrade-from-wave-2 they retire the superseded
-- trigger/function. These are NOT part of the ROLLBACK procedure below (that
-- section documents how to remove THIS revision's objects); they exist
-- solely for upgrade/replay idempotency.
-- ---------------------------------------------------------------------------
-- MIGRATION ORDER (exact):
--   1. (base schema + claim trigger drafts)
--   2. migrations/DRAFT_visual_global_history_20261002.sql   (exact-byte ledger)
--   3. migrations/DRAFT_visual_scene_phash_20261003.sql       (superseded sketch)
--   4. THIS FILE: migrations/DRAFT_visual_scene_claim_wave_20261003.sql
--   5. (any activation draft — must remain LAST; none exists yet)
-- This file must be applied only after DRAFT_visual_global_history_20261002.sql
-- (it references visual_group, visual_group_alias, visual_global_usage and the
-- visual_global_object_attestation attestation surface) and strictly before any
-- activation draft. It does not arm any guard: wiring into the resolved-key
-- claim path belongs to the activation draft (see ACTIVATION BLOCKERS).
--
-- ROLLBACK (before any activation): nothing is applied anywhere; delete the
-- file. If it were ever applied to a scratch database, drop in this order:
--   drop function if exists public.visual_scene_backfill_occupied();
--   drop function if exists public.visual_scene_approval_guarded(uuid);
--   drop function if exists public.visual_scene_publish_claim_guarded(uuid);
--   drop function if exists public.visual_scene_hold_reactivate(uuid,text);
--   drop function if exists public.visual_scene_hold_resolve(uuid,text,text,jsonb);
--   drop function if exists public.visual_scene_claim_decide(public.content_calendar,uuid);
--   drop function if exists public.visual_scene_claim_guard(public.content_calendar,uuid);
--   drop function if exists public.visual_scene_claim_scan(public.content_calendar,uuid);
--   drop function if exists public.visual_scene_row_candidate(public.content_calendar);
--   drop function if exists public.visual_scene_row_delivered_object(public.content_calendar);
--   drop function if exists public.visual_scene_write_holds(text,text,date,uuid,text,uuid,char(16),text,text,jsonb);
--   drop function if exists public.visual_scene_register_candidate(text,text,text,text,text,jsonb,text,text);
--   drop function if exists public.visual_scene_hamming(text,text);
--   drop trigger if exists visual_scene_occupied_immutable on public.visual_scene_phash_occupied;
--   drop trigger if exists visual_scene_candidate_immutable on public.visual_scene_candidate;
--   drop trigger if exists visual_scene_review_hold_guard on public.visual_scene_review_hold;
--   drop function if exists public.visual_scene_immutable();
--   drop function if exists public.visual_scene_review_hold_mutation();
--   drop table if exists public.visual_scene_review_hold;
--   drop table if exists public.visual_scene_phash_occupied;
--   drop table if exists public.visual_scene_candidate;
-- The frozen md5-keyed exact-byte ledger (visual_global_usage, its members and
-- the visual_global_claim_scene path) is NOT touched by this file and needs no
-- rollback.
--
-- WHAT THIS FILE IMPLEMENTS (redesign items (a)-(d) from
-- docs/VISUAL_SCENE_GUARD_DRAFT.md plus the 2026-10-04 P0 repair pass; item (e)
-- backfill remains a guarded stub):
--   (a) visual_scene_candidate — prep-time STAGING. A candidate row carries
--       owner-attested pHash evidence bound to exact verified bytes: it must
--       reference a visual_global_object_attestation row (same canonical
--       tenant + group + exact_url + md5 fingerprint), non-empty evidence, a
--       named actor, AND an object_role ('display' or 'poster') binding it to
--       the calendar row's exact DELIVERED object: the displayed image URL,
--       or the video poster/thumbnail URL when that is distinct from the
--       image URL (Sol P0-1). A bare tenant/group candidate is NEVER enough:
--       visual_scene_row_candidate resolves the candidate for a calendar row
--       by role + exact_url + fingerprint binding and the claim scan rejects
--       (fail closed) any candidate not bound to the row's delivered object.
--       A candidate NEVER counts as use and never gates.
--   (b) visual_scene_phash_occupied — a once-used scene is recorded ONLY by
--       the claim path (the merged exact-byte guard trigger, or the internal
--       decide/guard functions on a clean verdict), inside the same
--       transaction as the local/exact-byte sync. Occupancy is PERMANENT:
--       the append-only immutability trigger blocks UPDATE/DELETE, and no
--       function in this file frees an occupied scene on local release,
--       denial, or swap — mirroring the exact-byte ledger's "released stays
--       consumed" semantics (audit P1-3 / repair-spec occupancy persistence).
--   (c) visual_scene_review_hold — durable, reviewable holds written ONLY by
--       the non-raising claim paths (decide / the merged exact-byte guard
--       trigger's conflict branch, where the hold insertion is the LAST
--       write after visual_group_sync_row). A
--       partial unique index on
--       (tenant_id, group_key, claim_date, calendar_row_id, candidate_id,
--        candidate_phash, matched_phash, matched_tenant_id, matched_group_key,
--        matched_used_date) WHERE state='open' is the stable open-hold
--       uniqueness key (wave-2 item 2: exact candidate + matched occupied
--       identity): a retry of the same conflict for the same row inserts NO
--       duplicate hold (Sol P0-3), and a later DISTINCT conflict gets its own
--       hold instead of reusing an approval. Holds carry the full conflict
--       scope (claimant tenant/group/date/candidate+delivered object/phash +
--       matched tenant/group/date/phash); an APPROVED hold exempts ONLY that
--       exact reviewed conflict scope for that exact claim date — later or
--       different conflicts still hold.
--   (d) Server-side hamming comparison over the ENTIRE occupied table inside
--       the claim transaction (visual_scene_hamming is plpgsql over every
--       occupied row; there is deliberately NO exact-match pre-filter — a
--       one-bit-away pHash must be caught). No client-side scan remains a
--       valid decision path.
--   (e) visual_scene_backfill_occupied is a STUB that RAISES 0A000. Occupied
--       history must be derived from the exact-byte ledger before any
--       activation; shipping the backfill is part of the activation draft
--       (see ACTIVATION BLOCKERS).
--
-- POLICY (mirrors agent/visual_scene.py classify bands):
--   hamming <= 6    near_frame: cross-tenant OR same-tenant-different-date
--                   match BLOCKS the claim. Same-tenant same-date same-group
--                   (channel siblings of one reservation) remains LEGAL.
--   hamming 7..30   uncertain: creates a review hold AND BLOCKS the claim
--                   (fail closed; never silently passes).
--   hamming > 30    distinct: no effect.
--   unknown/mismatched candidate evidence for the claimed row => FAIL CLOSED.
-- pHash is SIMILARITY EVIDENCE ONLY. It never asserts byte identity, never
-- auto-approves, and never writes visual_group_scene_link, which remains
-- HUMAN-CONFIRMED ONLY via visual_group_link_scene.
--
-- === WAVE-2 REPAIRS (Astra SCENE_REPAIR_WAVE2_SCOPE.md, 2026-10-04) ==========
--  1. ONE AUTHORITATIVE CALENDAR BEFORE PATH. The trigger no longer skips
--     scene evaluation when new.visual_group_key was not pre-populated: it
--     resolves the canonical group itself via the existing exact-byte group
--     logic (visual_group_resolve_row, the same resolver the group guard
--     uses) and hydrates NEW before deciding. It also no longer treats a
--     scene with no staged candidate as inert: an engaged row without a
--     candidate bound to its exact delivered object is a HARD fail-closed
--     rejection, exactly like missing/invalid byte attestation. The exact
--     delivered-row byte attestation (visual_global_row_bytes_verified) is
--     validated BEFORE any hold write; a row that fails attestation raises
--     and never produces a hold. The previous "separate early trigger fires
--     before key resolution" design is REMOVED: this trigger is now the
--     single authoritative BEFORE path, so the activation-draft integration
--     hook into visual_group_guard_trigger is no longer required (old
--     ACTIVATION BLOCKER 1, resolved). The superseded sketch trigger/table
--     in DRAFT_visual_scene_phash_20261003.sql stays a non-write sketch.
--  2. ONE COMMITTED TRANSACTION. Hold insertion, the held-row mutation and
--     the old local membership release (visual_group_sync_row) all happen in
--     the one claim transaction that COMMITS the held row; no function on
--     the conflict path raises after a hold insert. The held mutation clears
--     BOTH publish_claim_token AND publish_reservation_day (the real
--     reservation column from calendar_publish_day_capacity_20260917.sql;
--     old ACTIVATION BLOCKER 3, resolved). The open-hold stable uniqueness
--     key now includes the exact candidate (candidate_id + candidate_phash)
--     AND the matched occupied identity (matched_phash + matched tenant /
--     group / used date), so a retry never duplicates and a later DISTINCT
--     conflict can never reuse an approval. candidate_id / exact_url /
--     fingerprint are now real hold columns (FK to the staged candidate),
--     not just evidence jsonb.
--  3. LOCK ORDER, UNCHANGED AND NOW TOTAL. Component locks first
--     (visual_group_lock_scene_components), then the single fleet-wide scene
--     advisory lock — for normal claims, hold resolution (scene tables only:
--     advisory alone, as before), reactivation (fixed: it now takes the
--     component locks BEFORE the advisory lock, and locks the held calendar
--     row FOR UPDATE before its re-scan), and the future backfill (must use
--     the same order; still a 0A000 stub). Resolutions now STORE the
--     reviewed actor (resolved_by, as before) AND the review evidence in the
--     new resolution_evidence column (one-shot, enforced by the row
--     trigger), and the approval exemption is scoped to the exact reviewed
--     candidate + delivered object (exact_url + fingerprint) + matched
--     occupied conflict.
--  4. STAGING STAYS SEPARATE FROM USAGE, MEDIA-KIND BOUND. The new
--     visual_scene_row_delivered_object helper derives the row's ACTUAL
--     media kind: a distinct thumbnail_url means a video row whose DISPLAYED
--     object is the poster, so ONLY a 'poster' candidate bound to that exact
--     thumbnail_url binds; otherwise the row is a photo row and ONLY a
--     'display' candidate bound to the exact image_url binds. A 'display'
--     candidate on the video file URL no longer satisfies a video row, and
--     no tenant/group-only candidate match exists anywhere. Both
--     visual_scene_row_candidate and visual_scene_claim_scan use this one
--     helper, so binding and scan can never disagree.
-- ===========================================================================
--
-- === WAVE-3 REPAIRS (Astra SCENE_REPAIR_WAVE3_SCOPE.md items 1-3, 2026-10-04;
-- Sol SCENE_AUDIT_GAPS_WAVE2.md and SCENE_WAVE3_SOL_DESIGN.md override the
-- wave-2 self-report; Sol's lock-order wording SUPERSEDES the scope file) ====
--  1. ONE AUTHORITATIVE CALENDAR BEFORE TRIGGER. The separate early scene
--     trigger content_calendar_scene_wave_claim_guard (installed by this file
--     in wave 2) is REMOVED: PostgreSQL runs same-kind triggers
--     alphabetically, so any later raising trigger could roll back the earlier
--     scene trigger's holds — that design failed the durable-hold contract.
--     The scene decision now runs INSIDE the existing exact-byte guard
--     (visual_group_guard_trigger in
--     DRAFT_visual_group_claim_trigger_20261002.sql), AFTER its unconditional
--     object/group resolution and component locking. The canonical group is
--     resolved UNCONDITIONALLY from the final delivered object there
--     (visual_group_resolve_row plus the changed-media refinement); a
--     caller-prefilled visual_group_key is a HINT ONLY, overwritten by the
--     resolved group, so a forged or stale hint can neither steer
--     claim/hold/occupancy nor bypass scene evaluation (audit "Authoritative
--     calendar key", PARTIAL/P0 — resolved). This file now provides ONLY the
--     scene tables, the scan/decide/guard decision core, the hold writer and
--     the review RPCs the merged guard and reviewers call.
--  2. NO RAISE AFTER HOLD (ordering invariant, realized in the merged
--     guard). The wave-2 conflict path inserted holds and THEN called
--     visual_group_sync_row; any raise there (or in the later exact-byte
--     BEFORE trigger) rolled back the hold AND the row (audit "Durable
--     committed hold", MISSING/P0). In the merged guard's conflict branch
--     every potentially-raising step completes BEFORE the final hold
--     insertion, in this exact order: (i) published/terminal and
--     ambiguous-flow checks, unconditional exact resolution and re-read,
--     candidate byte attestation, component locks, the fleet-wide advisory
--     lock and the scan; (ii) in-memory held-state mutation of NEW
--     (variant_status='archived', status='pending',
--     media_not_ready_reason='scene_review_hold', publish_claim_token AND
--     publish_reservation_day cleared); (iii) visual_group_sync_row(OLD, held
--     NEW, op) — may raise, no hold exists yet; (iv) the idempotent,
--     non-raising hold insertion as the LAST write; (v) RETURN NEW
--     immediately. A failure rolls back both calendar row and hold
--     (PostgreSQL has no autonomous-transaction escape hatch).
--  3. RESOLUTION/REACTIVATION LOCK ORDER MATCHES ACTUAL DML (Sol design,
--     superseding the scope file's component-before-fleet wording for these
--     two functions). visual_scene_hold_resolve and
--     visual_scene_hold_reactivate now lock in the order a normal UPDATE
--     takes locks: the CURRENT CALENDAR ROW FOR UPDATE FIRST, then the
--     scene-component locks (taken for BOTH the re-resolved identity and the
--     reviewed hold identity), then the single fleet-wide scene advisory lock
--     (component-before-fleet is preserved WITHIN that order). Both reload
--     the current calendar row and then RE-RESOLVE it UNCONDITIONALLY via
--     visual_group_resolve_row (hint-independent; Sol frozen-hash audit P0):
--     the re-resolved tenant/group — never a key persisted on the row or the
--     hold — drives the stale-binding and cross-tenant checks, the exact
--     candidate/role/URL/fingerprint/pHash rebind, and the live rescan.
--     Approval exempts ONLY
--     the exact reviewed conflict and does NOT activate the row.
--     Reactivation rescans and, after its UPDATE re-enters the merged
--     trigger, re-reads the PERSISTED row and reports failure
--     (reason='converted_back_to_held') unless the row is genuinely active —
--     a trigger may convert an attempted update back to held.
-- ===========================================================================
--
-- === HOLD-ROLLBACK: ENFORCED SEMANTICS (realized) ============================
-- A hold row exists IF AND ONLY IF the claim transaction commits in a
-- blocked/held state. This is now REALIZED by the committed held calendar row
-- inside the ONE authoritative BEFORE path (visual_group_guard_trigger): the
-- scene conflict branch NEVER raises — it mutates NEW to the held state
-- (variant_status='archived', status='pending',
-- media_not_ready_reason='scene_review_hold', publish_claim_token AND
-- publish_reservation_day cleared), passes the HELD new through
-- visual_group_sync_row so any old active local membership is released
-- BEFORE any hold exists, writes the idempotent hold evidence LAST (wave-3
-- item 2: no raise after hold), and RETURNS NEW so the transaction COMMITS
-- the held row with its holds. No exact claim and no pHash occupancy are
-- created for the held row. Because the conflict path never raises after the
-- hold write, the holds it wrote commit with the row — no caller replay, no
-- errdetail contract.
--   - visual_scene_claim_decide (NON-RAISING) is the internal function form of
--     the same contract for claim contexts that compose their own row write.
--   - visual_scene_claim_guard (RAISING, fail-closed error only) is kept for
--     hard-fail integration contexts. Because everything it does is rolled
--     back by its own raise (even through a plpgsql EXCEPTION handler's
--     savepoint — verified on scratch PG, 2026-10-03), NOTHING it does can be
--     durable; that is exactly why it writes NO hold rows. An integration
--     that raises gets no hold, by construction. Its errors still carry the
--     full match detail in errdetail for caller-side forensics.
-- Autonomous-transaction alternatives (a dblink self-connection writing the
-- hold from inside a raising path) were considered and REJECTED: they are
-- fragile under connection poolers (transaction-mode pooling breaks session
-- state and advisory-lock assumptions) and demand stored connect credentials
-- inside the database, which this security-definer surface must not require.
-- ===========================================================================
--
-- === EXECUTE REVOCATION (Sol P0-2 / Astra P0) ================================
-- Following the exact-byte precedent (visual_group_global_claim /
-- visual_group_global_release are trigger-internal), ALL scene claim-path
-- SECURITY DEFINER functions are revoked from public, anon, authenticated AND
-- service_role: visual_scene_claim_scan, visual_scene_claim_decide,
-- visual_scene_claim_guard, visual_scene_row_candidate,
-- visual_scene_row_delivered_object, visual_scene_write_holds and
-- visual_scene_backfill_occupied. (The wave-2 calendar trigger function
-- visual_scene_calendar_claim_guard no longer exists — the scene decision
-- lives inside visual_group_guard_trigger, revoked in its own file.)
-- Service callers can never fabricate
-- occupancy or holds outside the calendar transaction. The ONLY
-- service_role-executable scene entry points are the safe read/review
-- surfaces that cannot fabricate occupancy: visual_scene_hamming (pure),
-- visual_scene_register_candidate (prep-time staging; never consumes),
-- visual_scene_hold_resolve (validated one-shot open->terminal transition),
-- visual_scene_hold_reactivate (post-approval re-scan + row reactivation),
-- visual_scene_publish_claim_guarded (returns NULL for held rows) and
-- visual_scene_approval_guarded (excludes held rows). SELECT on the three
-- scene tables stays granted to service_role for review tooling.
-- ===========================================================================
--
-- === FLEET LOCK ORDER ========================================================
-- Occupancy writers and hold writers (the merged exact-byte guard's scene
-- branch and the internal decide/guard functions) use:
--   1. existing scene-component locks (visual_group_lock_scene_components,
--      taken by visual_group_guard_trigger / visual_group_sync_row),
--   2. THEN the single fleet-wide scene advisory lock
--      (hashtextextended('["visual_scene_global"]', 0)) inside
--      visual_scene_claim_scan.
-- The scan never takes component locks after the advisory lock; the merged
-- guard takes the component locks BEFORE the scan.
-- Hold resolution and reactivation (Sol SCENE_WAVE3_SOL_DESIGN.md, wave-3
-- item 3) use the order matching actual DML — a normal UPDATE already holds
-- the calendar row before its trigger seeks component locks, so
-- component/fleet-before-row can deadlock:
--   1. calendar row FOR UPDATE FIRST,
--   2. then the scene-component locks,
--   3. then the single fleet-wide scene advisory lock.
-- The future backfill must use the same component-then-fleet order (still a
-- 0A000 stub).
-- ===========================================================================
--
-- === ACTIVATION BLOCKERS (must be resolved by the activation draft) ==========
--  1. RESOLVED (wave-2 item 1, tightened wave-3 item 1). The ONE authoritative
--     calendar BEFORE path is now the exact-byte guard itself
--     (visual_group_guard_trigger): it resolves the canonical group
--     UNCONDITIONALLY from the final delivered object via
--     visual_group_resolve_row (a caller-prefilled visual_group_key is a hint
--     only, overwritten by the resolved group), validates the delivered-row
--     byte attestation before any hold write, and never skips scene
--     evaluation for an unstaged candidate (fail-closed rejection). The
--     separate early scene trigger is REMOVED, so no alphabetical trigger
--     ordering is involved: a held row never reaches the exact-byte claim as
--     claimable because the held mutation happens BEFORE
--     visual_group_sync_row inside the same trigger.
--  2. PUBLISH-CLAIM RPC WIRING. The real publish-claim RPC (PR230 path) is
--     not part of this draft stack; visual_scene_publish_claim_guarded
--     enforces the persisted-state contract (NULL for held rows) and raises
--     0A000 for the actual token mint. The activation draft must route the
--     real RPC through this persisted-state check. The approval RPC must
--     likewise consult persisted state (visual_scene_approval_guarded).
--  3. RESOLVED (wave-2 item 2). Production content_calendar carries the real
--     reservation column publish_reservation_day
--     (calendar_publish_day_capacity_20260917.sql); the held mutation clears
--     BOTH publish_claim_token AND publish_reservation_day.
--  4. BACKFILL (still 0A000). Occupied history must be derived from the
--     exact-byte ledger and attested candidates before activation. Unknown or
--     source-null published history must STAY HELD for review, and the
--     backfill may never mark a staged candidate as used. The backfill must
--     take the existing scene-component locks first, then the single
--     fleet-wide scene advisory lock (wave-2 item 3 lock order).
--  5. COVERAGE REPORT + LIVE ACCEPTANCE RUNS. visual_global_coverage /
--     visual_global_history_coverage must show reviewed coverage, and the
--     repair-spec acceptance suite (clean claim, legal sibling, cross-date /
--     cross-tenant near conflict, uncertain band, concurrent conflicting
--     claims, preparation without use, denial/swap, invalid attestation,
--     persisted hold, approval/claim false-success prevention, hold
--     resolution, backfill with unknown history) must be run live against a
--     disposable database by the independent reviewer on the exact diff.
-- ===========================================================================
begin;

-- ---------------------------------------------------------------------------
-- UPGRADE/REPLAY IDEMPOTENCY (Sol frozen-hash audit, P0): retire the WAVE-2
-- separate early scene trigger and its zero-argument trigger function. On a
-- clean bootstrap both drops are harmless no-op NOTICEs; on a database that
-- previously applied the wave-2 revision of this draft they remove the
-- leftovers, so the merged exact-byte guard
-- (content_calendar_visual_group_guard, installed by
-- DRAFT_visual_group_claim_trigger_20261002.sql) remains the ONE
-- authoritative content_calendar BEFORE path. Must run BEFORE any section
-- that depends on the merged guard being the only scene decision path.
-- The dropped signature matches the wave-2 creation exactly:
--   create or replace function public.visual_scene_calendar_claim_guard()
--     returns trigger ...
--   create trigger content_calendar_scene_wave_claim_guard before insert or
--     update on public.content_calendar for each row
--     execute function public.visual_scene_calendar_claim_guard();
-- ---------------------------------------------------------------------------
drop trigger if exists content_calendar_scene_wave_claim_guard on public.content_calendar;
drop function if exists public.visual_scene_calendar_claim_guard();

-- ---------------------------------------------------------------------------
-- (a) Prep-time candidate staging. Never counts as use. object_role binds the
-- candidate to the calendar row's exact DELIVERED object (Sol P0-1):
--   'display' — the displayed image (content_calendar.image_url);
--   'poster'  — the video poster/thumbnail (content_calendar.thumbnail_url),
--               only when distinct from image_url.
-- ---------------------------------------------------------------------------
create table if not exists public.visual_scene_candidate (
  candidate_id uuid primary key default gen_random_uuid(),
  tenant_id    text        not null,
  group_key    text        not null,
  object_role  text        not null default 'display' check (object_role in ('display','poster')),
  phash        char(16)    not null check (phash ~ '^[0-9a-f]{16}$'),
  exact_url    text        not null,
  fingerprint  text        not null check (fingerprint ~ '^md5:[0-9a-f]{32}$'),
  evidence     jsonb       not null check (jsonb_typeof(evidence) = 'object' and evidence <> '{}'::jsonb),
  attested_by  text        not null check (btrim(attested_by) <> ''),
  created_at   timestamptz not null default now(),
  foreign key (tenant_id, group_key) references public.visual_group(gym_id, group_key),
  foreign key (tenant_id, group_key, exact_url, fingerprint)
    references public.visual_global_object_attestation(tenant_id, group_key, exact_url, fingerprint)
);

comment on table public.visual_scene_candidate is
  'DRAFT/UNAPPLIED/OFF: prep-time pHash STAGING. Owner-attested similarity evidence bound to the exact verified DELIVERED object of a calendar row: object_role display=image_url, poster=thumbnail_url (when distinct), plus exact_url+fingerprint backed by visual_global_object_attestation (FK). A bare tenant/group match is never sufficient; a candidate NEVER counts as use and never gates.';

create index if not exists visual_scene_candidate_scene_idx
  on public.visual_scene_candidate (tenant_id, group_key);
create index if not exists visual_scene_candidate_phash_idx
  on public.visual_scene_candidate (phash);
create index if not exists visual_scene_candidate_object_idx
  on public.visual_scene_candidate (tenant_id, group_key, object_role, exact_url);

-- ---------------------------------------------------------------------------
-- (b) Occupied once-used scenes. Written ONLY inside the claim path; PERMANENT
-- (append-only, never freed on release/denial/swap). used_date NOT NULL so
-- the same-tenant cross-date policy always has a date dimension. Same-tenant
-- same-date same-group siblings are legal, so the PK includes the date and
-- re-recording a sibling is an idempotent no-op.
-- ---------------------------------------------------------------------------
create table if not exists public.visual_scene_phash_occupied (
  phash           char(16)    not null check (phash ~ '^[0-9a-f]{16}$'),
  tenant_id       text        not null,
  group_key       text        not null,
  used_date       date        not null,
  fingerprint     text        not null check (fingerprint ~ '^md5:[0-9a-f]{32}$'),
  calendar_row_id uuid,
  channel         text,
  evidence        jsonb       not null default '{}',
  created_at      timestamptz not null default now(),
  primary key (phash, tenant_id, group_key, used_date),
  foreign key (tenant_id, group_key) references public.visual_group(gym_id, group_key)
);

comment on table public.visual_scene_phash_occupied is
  'DRAFT/UNAPPLIED/OFF: once-used global scene (pHash) evidence, recorded ONLY inside the calendar claim transaction on a clean decision. PERMANENT: append-only, never freed on local release, denial or swap (mirrors the exact-byte ledger). Similarity evidence, never byte identity.';

-- Full-table hamming scans are the design (no selective index is correct for
-- a one-bit-away match); these indexes serve inspection and hold forensics.
create index if not exists visual_scene_phash_occupied_tenant_date_idx
  on public.visual_scene_phash_occupied (tenant_id, used_date);

-- ---------------------------------------------------------------------------
-- (c) Durable review holds. Written ONLY by the non-raising claim paths; a
-- hold is durable IF AND ONLY IF the claim transaction commits in a
-- blocked/held state (realized by the committed held calendar row; see
-- header). The partial unique index is the stable open-hold uniqueness key
-- (wave-2 item 2): it includes the exact candidate (candidate_id +
-- candidate_phash) AND the matched occupied identity (matched tenant/group/
-- date/phash), so retrying the same conflict for the same calendar row cannot
-- duplicate an open hold and a later DISTINCT conflict gets its own hold.
-- Identity columns double as the conflict scope for approval-scoped
-- exemption. Resolution columns (state, resolved_by, resolved_at,
-- resolution_evidence) may be set exactly once, from 'open' to a terminal
-- resolution, by the validated review RPC.
-- ---------------------------------------------------------------------------
create table if not exists public.visual_scene_review_hold (
  hold_id          uuid        primary key default gen_random_uuid(),
  tenant_id        text        not null,
  group_key        text        not null,
  claim_date       date        not null,
  calendar_row_id  uuid,
  channel          text,
  candidate_id     uuid        not null references public.visual_scene_candidate(candidate_id),
  exact_url        text        not null
    constraint visual_scene_review_hold_exact_url_ck check (btrim(exact_url) <> ''),
  fingerprint      text        not null
    constraint visual_scene_review_hold_fingerprint_ck check (fingerprint ~ '^md5:[0-9a-f]{32}$'),
  candidate_phash  char(16)    not null check (candidate_phash ~ '^[0-9a-f]{16}$'),
  matched_phash    char(16)    not null check (matched_phash ~ '^[0-9a-f]{16}$'),
  hamming          integer     not null check (hamming between 0 and 64),
  matched_tenant_id text       not null,
  matched_group_key text       not null,
  matched_used_date date       not null,
  hold_kind        text        not null check (hold_kind in ('near_frame','uncertain')),
  evidence         jsonb       not null default '{}',
  state            text        not null default 'open' check (state in ('open','approved','rejected')),
  resolved_by      text,
  resolved_at      timestamptz,
  resolution_evidence jsonb,
  created_at       timestamptz not null default now(),
  constraint visual_scene_review_hold_open_terminal_ck
    check ((state = 'open') = (resolved_by is null and resolved_at is null
                               and resolution_evidence is null))
);

-- ---------------------------------------------------------------------------
-- UPGRADE NORMALIZATION (Sol frozen-hash audit, P0, 2026-10-04): a database
-- that applied the WAVE-2 revision of this draft already has
-- visual_scene_review_hold, but WITHOUT candidate_id / exact_url /
-- fingerprint / resolution_evidence, with a WEAKER open-hold uniqueness
-- index (no candidate or matched-tenant identity) and an open/terminal check
-- that does not cover resolution_evidence. `create table if not exists` does
-- not evolve an existing table, so normalize it here:
--   1. add the missing columns (nullable first; the candidate FK included);
--   2. refuse to upgrade when legacy rows lack the candidate identity this
--      revision requires — a wave-2 hold without exact candidate evidence
--      cannot be carried into the wave-3 contract, and the draft upgrade is
--      deliberately NOT lossless (archive/re-drive such rows first);
--   3. enforce NOT NULL and the named checks (idempotent on clean bootstrap);
--   4. drop any legacy resolved_by check that ignores resolution_evidence
--      and any legacy open_uq index lacking candidate_id, then (re)create
--      the current versions below via IF NOT EXISTS.
-- On a clean bootstrap every step is a no-op.
-- ---------------------------------------------------------------------------
alter table public.visual_scene_review_hold
  add column if not exists candidate_id uuid
    references public.visual_scene_candidate(candidate_id),
  add column if not exists exact_url text,
  add column if not exists fingerprint text,
  add column if not exists resolution_evidence jsonb;
do $$
declare c record;
begin
  if exists(select 1 from public.visual_scene_review_hold
      where candidate_id is null or exact_url is null or fingerprint is null) then
    raise exception 'wave-2 scene review holds lack exact candidate identity; archive or re-drive them before applying this revision (draft upgrade is not lossless)'
      using errcode='23514';
  end if;
  alter table public.visual_scene_review_hold
    alter column candidate_id set not null,
    alter column exact_url set not null,
    alter column fingerprint set not null;
  -- Replace any legacy open/terminal check that ignores resolution_evidence.
  for c in select oid, conname from pg_constraint
    where conrelid = 'public.visual_scene_review_hold'::regclass and contype = 'c'
      and pg_get_constraintdef(oid) like '%resolved_by%'
      and pg_get_constraintdef(oid) not like '%resolution_evidence%' loop
    execute format('alter table public.visual_scene_review_hold drop constraint %I', c.conname);
  end loop;
  -- Ensure the named checks exist (no-ops on clean bootstrap).
  if not exists(select 1 from pg_constraint
      where conrelid = 'public.visual_scene_review_hold'::regclass
        and conname = 'visual_scene_review_hold_exact_url_ck') then
    alter table public.visual_scene_review_hold
      add constraint visual_scene_review_hold_exact_url_ck check (btrim(exact_url) <> '');
  end if;
  if not exists(select 1 from pg_constraint
      where conrelid = 'public.visual_scene_review_hold'::regclass
        and conname = 'visual_scene_review_hold_fingerprint_ck') then
    alter table public.visual_scene_review_hold
      add constraint visual_scene_review_hold_fingerprint_ck check (fingerprint ~ '^md5:[0-9a-f]{32}$');
  end if;
  if not exists(select 1 from pg_constraint
      where conrelid = 'public.visual_scene_review_hold'::regclass
        and conname = 'visual_scene_review_hold_open_terminal_ck') then
    alter table public.visual_scene_review_hold
      add constraint visual_scene_review_hold_open_terminal_ck
        check ((state = 'open') = (resolved_by is null and resolved_at is null
                                   and resolution_evidence is null));
  end if;
  -- Replace the legacy WEAKER open-hold uniqueness index (no candidate or
  -- matched-tenant identity) so the current definition below is created.
  if exists(select 1 from pg_class ic join pg_namespace ins on ins.oid = ic.relnamespace
      where ins.nspname = 'public' and ic.relname = 'visual_scene_review_hold_open_uq'
        and pg_get_indexdef(ic.oid) not like '%candidate_id%') then
    drop index public.visual_scene_review_hold_open_uq;
  end if;
end;
$$;

comment on table public.visual_scene_review_hold is
  'DRAFT/UNAPPLIED/OFF: reviewable scene-conflict holds. Written ONLY by the non-raising claim paths; durable IF AND ONLY IF the claim transaction commits in a blocked/held state (committed held calendar row). Stable open-hold uniqueness key (candidate + matched occupied identity) prevents retry duplicates and approval reuse. An approved hold exempts ONLY its exact reviewed candidate + delivered object + matched occupied conflict for its exact claim date. Resolution actor and evidence are stored one-shot (resolved_by / resolution_evidence).';

create index if not exists visual_scene_review_hold_state_idx
  on public.visual_scene_review_hold (state, created_at);
create index if not exists visual_scene_review_hold_scene_idx
  on public.visual_scene_review_hold (tenant_id, group_key, claim_date);
-- Stable open-hold uniqueness (Sol P0-3, wave-2 item 2): one open hold per
-- exact candidate + exact matched occupied identity per calendar row per
-- claim date. Retries conflict-do-nothing; a distinct later conflict inserts
-- its own hold and can never ride on an earlier approval.
create unique index if not exists visual_scene_review_hold_open_uq
  on public.visual_scene_review_hold
  (tenant_id, group_key, claim_date, calendar_row_id, candidate_id,
   candidate_phash, matched_phash, matched_tenant_id, matched_group_key,
   matched_used_date)
  where state = 'open';

-- ---------------------------------------------------------------------------
-- Immutability. Candidates and occupied rows are append-only history (occupied
-- is PERMANENT: release/denial/swap never frees a scene). Holds allow exactly
-- one open -> terminal transition of resolution columns only.
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_immutable()
returns trigger language plpgsql set search_path = public as $$
begin
  raise exception 'visual scene candidate/occupied evidence is append-only and immutable'
    using errcode='23514';
end;
$$;

create or replace function public.visual_scene_review_hold_mutation()
returns trigger language plpgsql set search_path = public as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'visual scene review holds are permanent' using errcode='23514';
  end if;
  if old.state <> 'open' or new.state = 'open'
      or new.hold_id is distinct from old.hold_id
      or new.tenant_id is distinct from old.tenant_id
      or new.group_key is distinct from old.group_key
      or new.claim_date is distinct from old.claim_date
      or new.calendar_row_id is distinct from old.calendar_row_id
      or new.channel is distinct from old.channel
      or new.candidate_id is distinct from old.candidate_id
      or new.exact_url is distinct from old.exact_url
      or new.fingerprint is distinct from old.fingerprint
      or new.candidate_phash is distinct from old.candidate_phash
      or new.matched_phash is distinct from old.matched_phash
      or new.hamming is distinct from old.hamming
      or new.matched_tenant_id is distinct from old.matched_tenant_id
      or new.matched_group_key is distinct from old.matched_group_key
      or new.matched_used_date is distinct from old.matched_used_date
      or new.hold_kind is distinct from old.hold_kind
      or new.evidence is distinct from old.evidence
      or new.created_at is distinct from old.created_at
      or nullif(btrim(new.resolved_by),'') is null
      or new.resolved_at is null
      or new.resolution_evidence is null
      or jsonb_typeof(new.resolution_evidence) <> 'object'
      or new.resolution_evidence = '{}'::jsonb then
    raise exception 'only a one-time open->terminal resolution may change a scene hold'
      using errcode='23514';
  end if;
  return new;
end;
$$;

drop trigger if exists visual_scene_candidate_immutable on public.visual_scene_candidate;
create trigger visual_scene_candidate_immutable
  before update or delete on public.visual_scene_candidate
  for each row execute function public.visual_scene_immutable();
drop trigger if exists visual_scene_occupied_immutable on public.visual_scene_phash_occupied;
create trigger visual_scene_occupied_immutable
  before update or delete on public.visual_scene_phash_occupied
  for each row execute function public.visual_scene_immutable();
drop trigger if exists visual_scene_review_hold_guard on public.visual_scene_review_hold;
create trigger visual_scene_review_hold_guard
  before update or delete on public.visual_scene_review_hold
  for each row execute function public.visual_scene_review_hold_mutation();

-- ---------------------------------------------------------------------------
-- (d) Server-side hamming distance over two 64-bit pHash hex strings.
-- Pure bit-string XOR; no bigint cast (high-bit values would overflow).
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_hamming(p_a text, p_b text)
returns integer language plpgsql immutable set search_path = public as $$
declare v_a bit(64); v_b bit(64);
begin
  if p_a is null or p_b is null
      or p_a !~ '^[0-9a-f]{16}$' or p_b !~ '^[0-9a-f]{16}$' then
    return null;
  end if;
  v_a := ('x' || p_a)::bit(64);
  v_b := ('x' || p_b)::bit(64);
  return length(replace((v_a # v_b)::text, '0', ''));
end;
$$;

-- ---------------------------------------------------------------------------
-- (a) Candidate registration (prep-time). Owner-attested: the exact DELIVERED
-- bytes must already be attested to THIS canonical tenant+group via
-- visual_global_object_attestation, evidence must carry verified_bytes
-- matching the attested md5, and object_role declares which delivered object
-- of the calendar row the candidate binds to. Registration NEVER consumes a
-- scene.
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_register_candidate(
  p_tenant text, p_group_key text, p_phash text,
  p_exact_url text, p_fingerprint text, p_evidence jsonb, p_actor text,
  p_object_role text default 'display'
) returns uuid language plpgsql security definer set search_path = public as $$
declare v_tenant text; v_id uuid;
begin
  v_tenant := public.visual_group_tenant_strict(p_tenant)::text;
  if p_tenant is distinct from v_tenant then
    raise exception 'canonical visual tenant required' using errcode='22023';
  end if;
  p_phash := lower(btrim(p_phash));
  p_fingerprint := lower(btrim(p_fingerprint));
  if p_phash is null or p_phash !~ '^[0-9a-f]{16}$'
      or nullif(btrim(p_group_key),'') is null
      or nullif(btrim(p_exact_url),'') is null
      or p_fingerprint is null or p_fingerprint !~ '^md5:[0-9a-f]{32}$'
      or p_evidence is null or jsonb_typeof(p_evidence) <> 'object'
      or p_evidence = '{}'::jsonb
      or p_evidence->>'verified_bytes' is distinct from p_fingerprint
      or nullif(btrim(p_actor),'') is null
      or p_object_role is null or p_object_role not in ('display','poster') then
    raise exception 'candidate needs canonical tenant, group, object role, phash, attested bytes and actor'
      using errcode='22023';
  end if;
  -- The phash must be attested against exact verified bytes owned by this
  -- tenant and scene. The FK enforces row existence; re-check tenant/group so
  -- an attestation from another scene can never back this candidate.
  if not exists(select 1 from public.visual_global_object_attestation o
      where o.tenant_id = v_tenant and o.group_key = p_group_key
        and o.exact_url = p_exact_url and o.fingerprint = p_fingerprint) then
    raise exception 'candidate phash is not backed by owner-attested exact bytes'
      using errcode='23514';
  end if;
  insert into public.visual_scene_candidate
    (tenant_id, group_key, object_role, phash, exact_url, fingerprint, evidence, attested_by)
    values (v_tenant, p_group_key, p_object_role, p_phash, p_exact_url, p_fingerprint,
            p_evidence, btrim(p_actor))
  returning candidate_id into v_id;
  return v_id;
end;
$$;

-- ---------------------------------------------------------------------------
-- (wave-2 item 4) Row media-kind + exact delivered object. Derives the row's
-- ACTUAL media kind: a non-blank thumbnail_url DISTINCT from image_url means
-- a video row whose DISPLAYED object is the poster ('poster' -> that exact
-- thumbnail_url); anything else is a photo row whose displayed object is the
-- image itself ('display' -> the exact image_url). Returns 0 rows when the
-- row has no displayable object. BOTH the row-candidate binding and the
-- claim scan use this one helper, so display-vs-poster binding can never
-- disagree with the scan and no tenant/group-only match is possible.
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_row_delivered_object(
  p_row public.content_calendar
) returns table(object_role text, exact_url text)
language plpgsql stable set search_path = public as $$
declare v_img text; v_thumb text;
begin
  v_img := nullif(btrim(p_row.image_url), '');
  v_thumb := nullif(btrim(to_jsonb(p_row)->>'thumbnail_url'), '');
  if v_thumb is not null and v_thumb is distinct from v_img then
    object_role := 'poster'; exact_url := v_thumb; return next;
  elsif v_img is not null then
    object_role := 'display'; exact_url := v_img; return next;
  end if;
  return;
end;
$$;

-- ---------------------------------------------------------------------------
-- (a)+(P0-1)+(wave-2 item 4) Row-to-candidate binding. Resolves THE staged
-- candidate backing a calendar row by the row's ACTUAL media kind and exact
-- DELIVERED object (visual_scene_row_delivered_object): a video row binds
-- ONLY a 'poster' candidate on its exact thumbnail_url; a photo row binds
-- ONLY a 'display' candidate on its exact image_url. A 'display' candidate
-- on the video file URL never satisfies a video row. The candidate's
-- (tenant, group, exact_url, fingerprint) is already FK-enforced against
-- visual_global_object_attestation. Returns NULL when no candidate is bound;
-- raises fail-closed if MORE THAN ONE distinct pHash is staged against the
-- same delivered object (ambiguous evidence).
-- Trigger/internal only (EXECUTE revoked from all non-owner roles).
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_row_candidate(
  p_row public.content_calendar
) returns uuid language plpgsql stable security definer set search_path = public as $$
declare v_tenant text; v_obj record; v_ids uuid[]; v_phashes text[];
begin
  v_tenant := public.visual_group_tenant_id(p_row.gym_id)::text;
  if v_tenant is null or p_row.visual_group_key is null then
    return null;
  end if;
  select d.object_role, d.exact_url into v_obj
    from public.visual_scene_row_delivered_object(p_row) d;
  if v_obj.exact_url is null then
    return null;
  end if;
  select array_agg(c.candidate_id order by c.candidate_id),
    array_agg(distinct c.phash)
    into v_ids, v_phashes
    from public.visual_scene_candidate c
    where c.tenant_id = v_tenant and c.group_key = p_row.visual_group_key
      and c.object_role = v_obj.object_role
      and c.exact_url = v_obj.exact_url;
  if v_ids is null then
    return null;
  end if;
  if array_length(v_phashes, 1) > 1 then
    raise exception 'conflicting staged scene candidates for one delivered object; scene guard fails closed'
      using errcode='23514';
  end if;
  return v_ids[1];
end;
$$;

-- ---------------------------------------------------------------------------
-- (b)+(c)+(d) Claim-path decision core. INTERNAL (EXECUTE revoked from
-- public/anon/authenticated/service_role). Takes the CALENDAR ROW (not a bare
-- tenant/group), so every decision is bound to the row's exact delivered
-- object (Sol P0-1). Does canonical-scene validation, the single fleet-wide
-- advisory lock (taken AFTER the caller's scene-component locks — see FLEET
-- LOCK ORDER — and never instead of them), the mandatory bound-candidate
-- lookup, and the full occupied-table hamming scan (no exact-match
-- pre-filter). Writes NOTHING. o_worst_band values:
--   null          clean (or only legal same-tenant same-date same-group
--                 siblings, or conflicts exempted by an APPROVED hold for
--                 exactly this reviewed pair and claim date);
--                 caller records the occupied scene.
--   'near_frame'  hamming <= 6 conflict (cross-tenant any date, or
--                 same-tenant different date) — blocked.
--   'uncertain'   hamming 7..30 conflict — blocked with review.
--   'fail_closed' no candidate bound to this row's delivered object —
--                 blocked fail-closed.
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_claim_scan(
  p_row public.content_calendar, p_candidate_id uuid,
  out o_tenant text, out o_candidate_id uuid, out o_phash char(16),
  out o_fingerprint text, out o_exact_url text,
  out o_worst_band text, out o_detail jsonb
) language plpgsql security definer set search_path = public as $$
declare v_candidate public.visual_scene_candidate%rowtype;
  v_match record;
begin
  o_detail := '[]'::jsonb;
  -- Wave-2 item 1: resolve the canonical tenant from the row's raw calendar
  -- key. visual_group_tenant_strict RAISES for an unmapped key, so a raw
  -- alias key resolves here and never has to be pre-canonicalized on the
  -- row (the exact-byte stack deliberately keeps calendar keys raw). All
  -- internal scene keys below use the resolved canonical tenant.
  o_tenant := public.visual_group_tenant_strict(p_row.gym_id)::text;
  if nullif(btrim(p_row.visual_group_key),'') is null
      or p_row.post_date is null
      or not exists(select 1 from public.visual_group
        where gym_id = o_tenant and group_key = p_row.visual_group_key) then
    raise exception 'scene claim guard needs a canonical scene and date'
      using errcode='22023';
  end if;
  -- Serialize ALL scene-claim comparisons fleet-wide for this transaction.
  -- Lock order: the caller's scene-component locks come FIRST; this is always
  -- the single global scene advisory lock, second and last.
  if not pg_try_advisory_xact_lock(hashtextextended(
      jsonb_build_array('visual_scene_global')::text, 0)) then
    raise exception 'global scene claim busy; retry transaction' using errcode='55P03';
  end if;
  -- Bound candidate (Sol P0-1, wave-2 item 4): the candidate must belong to
  -- THIS scene AND bind to THIS row's actual media kind + exact delivered
  -- object (visual_scene_row_delivered_object: poster for video rows,
  -- display for photo rows). Unknown or mismatched = fail closed.
  select c.* into v_candidate from public.visual_scene_candidate c
    join public.visual_scene_row_delivered_object(p_row) d
      on d.object_role = c.object_role and d.exact_url = c.exact_url
    where c.candidate_id = p_candidate_id
      and c.tenant_id = o_tenant and c.group_key = p_row.visual_group_key;
  if v_candidate.candidate_id is null then
    o_worst_band := 'fail_closed';
    return;
  end if;
  o_candidate_id := v_candidate.candidate_id;
  o_phash := v_candidate.phash;
  o_fingerprint := v_candidate.fingerprint;
  o_exact_url := v_candidate.exact_url;
  -- (d) Server-side hamming over the ENTIRE occupied table. No exact-match
  -- pre-filter: every occupied scene/date/tenant is compared.
  for v_match in
    select o.phash, o.tenant_id, o.group_key, o.used_date,
      public.visual_scene_hamming(v_candidate.phash, o.phash) as dist
    from public.visual_scene_phash_occupied o
  loop
    if v_match.dist is null or v_match.dist > 30 then
      continue;
    end if;
    -- Same-tenant same-date same-group siblings are legal; they are not
    -- conflicts (re-recording below is an idempotent no-op).
    if v_match.tenant_id = o_tenant and v_match.used_date = p_row.post_date
        and v_match.group_key = p_row.visual_group_key then
      continue;
    end if;
    -- Approval-scoped exemption (wave-2 items 2+3): an APPROVED hold exempts
    -- ONLY its exact reviewed scope — claimant tenant/group/date, the exact
    -- reviewed candidate AND delivered object (candidate_id + exact_url +
    -- fingerprint + phash), and the exact matched occupied identity. Later
    -- or different conflicts still hold.
    if exists(select 1 from public.visual_scene_review_hold h
        where h.state = 'approved'
          and h.tenant_id = o_tenant and h.group_key = p_row.visual_group_key
          and h.claim_date = p_row.post_date
          and h.candidate_id = v_candidate.candidate_id
          and h.exact_url = v_candidate.exact_url
          and h.fingerprint = v_candidate.fingerprint
          and h.candidate_phash = v_candidate.phash
          and h.matched_phash = v_match.phash
          and h.matched_tenant_id = v_match.tenant_id
          and h.matched_group_key = v_match.group_key
          and h.matched_used_date = v_match.used_date) then
      continue;
    end if;
    o_detail := o_detail || jsonb_build_array(jsonb_build_object(
      'phash', v_match.phash, 'tenant_id', v_match.tenant_id,
      'group_key', v_match.group_key, 'used_date', v_match.used_date,
      'hamming', v_match.dist,
      'band', case when v_match.dist <= 6 then 'near_frame' else 'uncertain' end));
    if v_match.dist <= 6 then
      -- near_frame blocks both cross-tenant and same-tenant-other-date.
      o_worst_band := 'near_frame';
    elsif o_worst_band is null then
      o_worst_band := 'uncertain';
    end if;
  end loop;
end;
$$;

-- ---------------------------------------------------------------------------
-- (c) Shared IDEMPOTENT hold writer (Sol P0-3, wave-2 item 2). One open hold
-- per exact candidate + exact matched occupied identity per calendar row per
-- claim date; retries insert no duplicate (stable open-hold uniqueness key),
-- and a later DISTINCT conflict inserts its own hold. Returns the hold_ids
-- actually inserted by THIS call (skipped duplicates are not reported
-- again). INTERNAL. Never raises for a claim outcome.
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_write_holds(
  p_tenant text, p_group_key text, p_claim_date date,
  p_row_id uuid, p_channel text, p_candidate_id uuid,
  p_candidate_phash char(16), p_fingerprint text, p_exact_url text,
  p_detail jsonb
) returns jsonb language plpgsql security definer set search_path = public as $$
declare v_match record; v_hold_ids jsonb := '[]'::jsonb; v_hold_id uuid;
begin
  for v_match in
    select (m->>'phash')::char(16) as phash, m->>'tenant_id' as tenant_id,
      m->>'group_key' as group_key, (m->>'used_date')::date as used_date,
      (m->>'hamming')::integer as dist, m->>'band' as band
    from jsonb_array_elements(p_detail) m
    order by (m->>'hamming')::integer, m->>'phash', m->>'tenant_id'
  loop
    insert into public.visual_scene_review_hold
      (tenant_id, group_key, claim_date, calendar_row_id, channel,
       candidate_id, exact_url, fingerprint,
       candidate_phash, matched_phash, hamming, matched_tenant_id,
       matched_group_key, matched_used_date, hold_kind, evidence)
      values (p_tenant, p_group_key, p_claim_date, p_row_id, p_channel,
        p_candidate_id, p_exact_url, p_fingerprint,
        p_candidate_phash, v_match.phash, v_match.dist, v_match.tenant_id,
        v_match.group_key, v_match.used_date, v_match.band,
        jsonb_build_object('candidate_id', p_candidate_id,
          'fingerprint', p_fingerprint, 'exact_url', p_exact_url))
      on conflict (tenant_id, group_key, claim_date, calendar_row_id,
                   candidate_id, candidate_phash, matched_phash,
                   matched_tenant_id, matched_group_key, matched_used_date)
      where state = 'open'
      do nothing
    returning hold_id into v_hold_id;
    if v_hold_id is not null then
      v_hold_ids := v_hold_ids || jsonb_build_array(v_hold_id);
    end if;
  end loop;
  return v_hold_ids;
end;
$$;

-- ---------------------------------------------------------------------------
-- NON-RAISING decide variant — INTERNAL (EXECUTE revoked from all non-owner
-- roles; the calendar trigger is the intended caller). Same locks, same full
-- occupied-table hamming scan, same bands as the raising guard. It NEVER
-- raises for a claim outcome; it returns a jsonb decision object and lets the
-- caller commit the appropriate state:
--   {decision:'claimed', ...}                  clean; the occupied scene is
--                                              recorded in THIS transaction.
--   {decision:'blocked', reason, matches:[...],
--    hold_ids:[...]}                           idempotent hold row(s) written —
--                                              durable IF AND ONLY IF the caller
--                                              COMMITS the blocked claim as a
--                                              held calendar row.
-- On ANY blocked decision it NEVER writes visual_scene_phash_occupied and
-- NEVER touches candidate used status (candidates never count as use
-- regardless), so a rejected claim leaves no false "used" mark. Malformed
-- input (non-canonical scene/date, busy lock) still raises — that is a
-- caller bug or retry signal, not a claim outcome.
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_claim_decide(
  p_row public.content_calendar, p_candidate_id uuid
) returns jsonb language plpgsql security definer set search_path = public as $$
declare v_scan record; v_hold_ids jsonb := '[]'::jsonb;
begin
  select * into v_scan from public.visual_scene_claim_scan(p_row, p_candidate_id);
  if v_scan.o_worst_band = 'fail_closed' then
    return jsonb_build_object('decision', 'blocked',
      'reason', 'fail_closed_no_bound_candidate',
      'matches', '[]'::jsonb, 'hold_ids', '[]'::jsonb,
      'tenant_id', v_scan.o_tenant, 'group_key', p_row.visual_group_key,
      'claim_date', p_row.post_date, 'candidate_id', p_candidate_id,
      'calendar_row_id', p_row.id);
  end if;
  if v_scan.o_worst_band is not null then
    v_hold_ids := public.visual_scene_write_holds(v_scan.o_tenant,
      p_row.visual_group_key, p_row.post_date, p_row.id, p_row.account,
      v_scan.o_candidate_id, v_scan.o_phash, v_scan.o_fingerprint,
      v_scan.o_exact_url, v_scan.o_detail);
    return jsonb_build_object('decision', 'blocked',
      'reason', case v_scan.o_worst_band
        when 'near_frame' then 'near_frame_conflict'
        else 'uncertain_match_review' end,
      'matches', v_scan.o_detail, 'hold_ids', v_hold_ids,
      'phash', v_scan.o_phash, 'tenant_id', v_scan.o_tenant,
      'group_key', p_row.visual_group_key, 'claim_date', p_row.post_date,
      'candidate_id', v_scan.o_candidate_id, 'calendar_row_id', p_row.id);
  end if;
  -- Clean (or only legal same-scene siblings / approved-pair exemptions):
  -- record the used scene atomically in THIS claim transaction.
  insert into public.visual_scene_phash_occupied
    (phash, tenant_id, group_key, used_date, fingerprint,
     calendar_row_id, channel, evidence)
    values (v_scan.o_phash, v_scan.o_tenant, p_row.visual_group_key, p_row.post_date,
      v_scan.o_fingerprint, p_row.id, p_row.account,
      jsonb_build_object('candidate_id', v_scan.o_candidate_id,
        'exact_url', v_scan.o_exact_url))
    on conflict (phash, tenant_id, group_key, used_date) do nothing;
  return jsonb_build_object('decision', 'claimed', 'phash', v_scan.o_phash,
    'tenant_id', v_scan.o_tenant, 'group_key', p_row.visual_group_key,
    'used_date', p_row.post_date, 'candidate_id', v_scan.o_candidate_id,
    'calendar_row_id', p_row.id);
end;
$$;

-- ---------------------------------------------------------------------------
-- RAISING guard variant — INTERNAL, fail-closed error only, for hard-fail
-- integration contexts. Because it RAISES on any non-clean outcome, NOTHING
-- it does can be durable: PostgreSQL rolls back its work on the raise (even
-- through a plpgsql EXCEPTION handler's savepoint). That is exactly why it
-- writes NO visual_scene_review_hold rows — by construction an integration
-- that raises gets no hold; hold durability belongs exclusively to the
-- non-raising committed-held-row path. Same locks, same full occupied-table
-- hamming scan, same bands; errors carry the full match detail in errdetail.
-- On a clean verdict it records the occupied scene in THIS transaction:
--   'claimed'            scene recorded as occupied; claim may proceed.
--   raises 'blocked'     near_frame conflict (cross-tenant any date, or
--                        same-tenant different date) — no hold written.
--   raises 'hold'        any 7..30 uncertain match — no hold written.
--   raises 'fail_closed' no candidate bound to this row's delivered object.
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_claim_guard(
  p_row public.content_calendar, p_candidate_id uuid
) returns jsonb language plpgsql security definer set search_path = public as $$
declare v_scan record;
begin
  select * into v_scan from public.visual_scene_claim_scan(p_row, p_candidate_id);
  if v_scan.o_worst_band = 'fail_closed' then
    raise exception 'no scene candidate bound to this row''s delivered object; scene guard fails closed'
      using errcode='23514';
  end if;
  if v_scan.o_worst_band = 'near_frame' then
    raise exception 'scene near-frame conflict blocks the visual claim'
      using errcode='23514', detail = v_scan.o_detail::text,
        hint = 'raising guard writes no hold by construction; the committed held-row path (calendar trigger) owns durable holds';
  end if;
  if v_scan.o_worst_band = 'uncertain' then
    raise exception 'uncertain scene match requires review; visual claim held'
      using errcode='23514', detail = v_scan.o_detail::text,
        hint = 'raising guard writes no hold by construction; the committed held-row path (calendar trigger) owns durable holds';
  end if;
  -- Clean (or only legal same-scene siblings / approved-pair exemptions):
  -- record the used scene atomically in THIS claim transaction.
  insert into public.visual_scene_phash_occupied
    (phash, tenant_id, group_key, used_date, fingerprint,
     calendar_row_id, channel, evidence)
    values (v_scan.o_phash, v_scan.o_tenant, p_row.visual_group_key, p_row.post_date,
      v_scan.o_fingerprint, p_row.id, p_row.account,
      jsonb_build_object('candidate_id', v_scan.o_candidate_id,
        'exact_url', v_scan.o_exact_url))
    on conflict (phash, tenant_id, group_key, used_date) do nothing;
  return jsonb_build_object('verdict', 'claimed', 'phash', v_scan.o_phash,
    'tenant_id', v_scan.o_tenant, 'group_key', p_row.visual_group_key,
    'used_date', p_row.post_date, 'candidate_id', v_scan.o_candidate_id,
    'calendar_row_id', p_row.id);
end;
$$;

-- ---------------------------------------------------------------------------
-- WAVE-3 ARCHITECTURE (Astra SCENE_REPAIR_WAVE3_SCOPE.md item 1; Sol
-- SCENE_WAVE3_SOL_DESIGN.md, 2026-10-04): there is NO separate scene calendar
-- trigger in this file anymore. The wave-2 trigger
-- content_calendar_scene_wave_claim_guard is REMOVED: PostgreSQL runs
-- same-kind triggers alphabetically, so any later raising trigger (the
-- exact-byte guard) could roll back holds written by the earlier scene
-- trigger, breaking the durable-hold contract (audit "Durable committed
-- hold", MISSING/P0). The scene decision — unconditional group resolution
-- from the final delivered object, byte attestation, bound-candidate
-- requirement, component-before-fleet locking, the occupied-table scan, the
-- held-state mutation, sync and the LAST-write hold insertion — now lives
-- INSIDE the exact-byte guard visual_group_guard_trigger in
-- migrations/DRAFT_visual_group_claim_trigger_20261002.sql, after its
-- unconditional object/group resolution and component locking, with the
-- ordering invariant: every potentially-raising step completes before the
-- final idempotent hold insertion, which is immediately followed by
-- RETURN NEW. This file provides the scene tables, the scan/decide/guard
-- core and the review RPCs that the merged guard and reviewers call.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- (P0-5) CALLER-STATE CONTRACT. Both wrappers read PERSISTED row state, never
-- attempted-update success.
-- visual_scene_publish_claim_guarded: returns NULL (no token) when the row
-- persisted as HELD (status='pending', variant_status='archived',
-- media_not_ready_reason='scene_review_hold'). For a non-held row the actual
-- token mint belongs to the real publish-claim RPC (PR230), which is not part
-- of this draft stack: 0A000 pending calendar integration (ACTIVATION
-- BLOCKER 2).
-- visual_scene_approval_guarded: the approval-eligibility check the approval
-- RPC must apply — a held (or otherwise media-not-ready) row can never be
-- returned as approved.
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_publish_claim_guarded(
  p_row_id uuid
) returns uuid language plpgsql stable security definer set search_path = public as $$
declare v_row public.content_calendar%rowtype;
begin
  select * into v_row from public.content_calendar where id = p_row_id;
  if not found then
    raise exception 'calendar row not found' using errcode='P0002';
  end if;
  if v_row.status = 'pending' and v_row.variant_status = 'archived'
      and v_row.media_not_ready_reason = 'scene_review_hold' then
    return null;
  end if;
  raise exception 'visual_scene_publish_claim_guarded: token mint is pending calendar integration (activation draft)'
    using errcode='0A000';
end;
$$;

create or replace function public.visual_scene_approval_guarded(
  p_row_id uuid
) returns boolean language plpgsql stable security definer set search_path = public as $$
declare v_row public.content_calendar%rowtype;
begin
  select * into v_row from public.content_calendar where id = p_row_id;
  if not found then
    raise exception 'calendar row not found' using errcode='P0002';
  end if;
  -- Held rows (and any not-ready or unresolved rows) are never approvable.
  return not (v_row.media_not_ready_reason is not null
    or (v_row.status = 'pending' and v_row.variant_status = 'archived')
    or v_row.visual_group_key is null or v_row.post_date is null
    or nullif(btrim(v_row.image_url),'') is null);
end;
$$;

-- ---------------------------------------------------------------------------
-- (P0-7, wave-3 item 3) HOLD REVIEW PATH. Validated one-shot
-- open->approved|rejected with a non-empty actor and non-empty evidence jsonb
-- (the row trigger enforces the one-shot transition). LOCK ORDER (Sol
-- SCENE_WAVE3_SOL_DESIGN.md, superseding the broader scope wording): the
-- hold identity is read first (unlocked), then the CURRENT CALENDAR ROW is
-- locked FOR UPDATE FIRST, then the scene-component locks, then the single
-- fleet-wide scene advisory lock — matching actual DML, where a normal
-- UPDATE already holds the row before its trigger seeks component locks, so
-- component/fleet-before-row can deadlock. Component-before-fleet is
-- preserved within that order. Immediately after the row lock the row is
-- RE-RESOLVED UNCONDITIONALLY (visual_group_resolve_row plus the canonical
-- tenant of its raw calendar key — hint-independent, Sol frozen-hash audit
-- P0); the component locks cover BOTH the re-resolved identity and the
-- reviewed hold identity, and the hold is then re-read FOR UPDATE. Before
-- ANY resolution takes effect the LIVE row is rebound and rescanned UNDER
-- THE RE-RESOLVED GROUP (never a key persisted on the row or the hold): the
-- row
-- must still be in the reviewed held state on the reviewed tenant/group/date
-- (stale review and cross-tenant review are refused), the exact
-- delivered-object candidate evidence must still bind to the live row with a
-- valid byte attestation, the reviewed candidate and matched occupied
-- identities must be unchanged, and a fresh visual_scene_claim_scan of the
-- live row must still surface the exact reviewed matched conflict at the
-- exact reviewed hamming distance — any conflict-scope drift rejects the
-- resolution. An approval exempts ONLY the specifically reviewed conflict
-- pair for its exact claim date (enforced in visual_scene_claim_scan) and
-- does NOT activate the row; later or different conflicts still hold.
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_hold_resolve(
  p_hold_id uuid, p_decision text, p_actor text, p_evidence jsonb
) returns jsonb language plpgsql security definer set search_path = public as $$
declare v_hold public.visual_scene_review_hold%rowtype;
  v_candidate public.visual_scene_candidate%rowtype;
  v_row public.content_calendar%rowtype;
  v_scan record;
  v_hamming integer;
  v_row_tenant text; v_resolved text;
begin
  if p_decision is null or p_decision not in ('approved','rejected')
      or nullif(btrim(p_actor),'') is null
      or p_evidence is null or jsonb_typeof(p_evidence) <> 'object'
      or p_evidence = '{}'::jsonb then
    raise exception 'hold resolution needs decision approved|rejected, a named actor and evidence'
      using errcode='22023';
  end if;
  -- Read the hold identity first (no lock yet) so the calendar row can be
  -- locked FIRST (row -> component -> fleet, wave-3 item 3; the wave-2 form
  -- took the fleet lock without the component locks and never read the row).
  select * into v_hold from public.visual_scene_review_hold
    where hold_id = p_hold_id;
  if not found then
    raise exception 'scene hold not found' using errcode='P0002';
  end if;
  if v_hold.calendar_row_id is null then
    raise exception 'hold has no calendar row to rebind; refusing stale resolution'
      using errcode='23514';
  end if;
  -- 1. Calendar row FOR UPDATE FIRST — matches actual DML lock order (a
  -- normal UPDATE already holds the row before its trigger seeks component
  -- locks; component/fleet-before-row can deadlock against it).
  select * into v_row from public.content_calendar
    where id = v_hold.calendar_row_id for update;
  if not found then
    raise exception 'held calendar row no longer present; refusing stale resolution'
      using errcode='23514';
  end if;
  -- RE-RESOLVE the locked current row UNCONDITIONALLY from its current
  -- delivered object (Sol frozen-hash audit, P0): the canonical tenant comes
  -- from the row's raw calendar key, and the group comes from
  -- visual_group_resolve_row — neither the persisted visual_group_key on the
  -- row NOR the group stored on the hold is authority for the checks below.
  v_row_tenant := public.visual_group_tenant_id(v_row.gym_id)::text;
  v_resolved := public.visual_group_resolve_row(v_row);
  -- 2. Scene-component locks for BOTH the re-resolved identity and the
  -- reviewed hold identity (same dual-closure shape the merged guard uses),
  -- THEN 3. the single fleet-wide advisory lock.
  perform public.visual_group_lock_scene_components(jsonb_build_array(
    jsonb_build_object('gym_id', v_row_tenant, 'group_key', v_resolved),
    jsonb_build_object('gym_id', v_hold.tenant_id, 'group_key', v_hold.group_key)));
  if not pg_try_advisory_xact_lock(hashtextextended(
      jsonb_build_array('visual_scene_global')::text, 0)) then
    raise exception 'global scene claim busy; retry transaction' using errcode='55P03';
  end if;
  -- POST-LOCK STABILITY (Sol independent audit P0, 2026-10-04): the
  -- resolution was computed before the component locks; the alias evidence
  -- behind it is not covered by the calendar row lock. Re-resolve under the
  -- full lock set and REQUIRE the identity to be unchanged — an object or
  -- alias swapped between the row lock and the component/fleet locks must
  -- never be approved on the pre-lock identity.
  if public.visual_group_resolve_row(v_row) is distinct from v_resolved
      or public.visual_group_tenant_id(v_row.gym_id)::text is distinct from v_row_tenant then
    raise exception 'visual identity changed while locking; retry transaction' using errcode='55P03';
  end if;
  -- Re-read the hold under the locks and re-verify its state.
  select * into v_hold from public.visual_scene_review_hold
    where hold_id = p_hold_id for update;
  if not found then
    raise exception 'scene hold not found' using errcode='P0002';
  end if;
  if v_hold.state <> 'open' then
    raise exception 'scene hold is already resolved' using errcode='23514';
  end if;
  -- Cross-tenant review is refused: the row's CURRENT canonical tenant
  -- (re-resolved above) must equal the reviewed hold tenant.
  if v_row_tenant is null or v_row_tenant is distinct from v_hold.tenant_id then
    raise exception 'calendar row moved tenants since the hold; cross-tenant review refused'
      using errcode='23514';
  end if;
  -- Stale review is refused: the group RE-RESOLVED from the row's current
  -- delivered object must still be the reviewed group, and the row must
  -- still sit in the exact reviewed held state on the reviewed claim date.
  if v_resolved is null or v_resolved is distinct from v_hold.group_key
      or v_row.post_date is distinct from v_hold.claim_date
      or not (v_row.status = 'pending' and v_row.variant_status = 'archived'
              and v_row.media_not_ready_reason = 'scene_review_hold') then
    raise exception 'calendar row mutated since the hold; refusing stale resolution'
      using errcode='23514';
  end if;
  -- Drive every subsequent check and the rescan from the RE-RESOLVED
  -- identity, never from a key persisted on the row or the hold.
  v_row.visual_group_key := v_resolved;
  -- BOUND-CANDIDATE IDENTITY (Sol independent audit P1, 2026-10-04): the
  -- reviewed candidate must be the UNIQUE candidate bound to the live row's
  -- delivered object. If a second candidate now binds the same object —
  -- even with identical evidence — the binding set differs from what was
  -- reviewed, and (with conflicting pHashes) visual_scene_row_candidate
  -- raises the ambiguous-evidence fail-closed error, which propagates.
  -- Approving would exempt a conflict the reviewer never saw; refuse.
  if (select count(*) from public.visual_scene_candidate c
      join public.visual_scene_row_delivered_object(v_row) d
        on d.object_role = c.object_role and d.exact_url = c.exact_url
      where c.tenant_id = v_hold.tenant_id and c.group_key = v_resolved) <> 1
      or public.visual_scene_row_candidate(v_row) is distinct from v_hold.candidate_id then
    raise exception 'bound scene candidate for the live row is not uniquely the reviewed candidate; refusing resolution'
      using errcode='23514';
  end if;
  -- Rebind the exact delivered-object evidence against the LIVE row: the
  -- reviewed candidate must still bind to the row's actual delivered object
  -- (role + exact_url) for the reviewed tenant/group, and the row's exact
  -- delivered bytes must still be attested.
  if not exists(select 1 from public.visual_scene_row_delivered_object(v_row) d
      join public.visual_scene_candidate c
        on c.object_role = d.object_role and c.exact_url = d.exact_url
      where c.candidate_id = v_hold.candidate_id
        and c.tenant_id = v_hold.tenant_id and c.group_key = v_hold.group_key
        and d.exact_url = v_hold.exact_url) then
    raise exception 'delivered object no longer binds the reviewed candidate; hold conflict scope drifted'
      using errcode='23514';
  end if;
  if not public.visual_global_row_bytes_verified(v_row) then
    raise exception 'delivered-row byte attestation no longer valid; hold conflict scope drifted'
      using errcode='23514';
  end if;
  -- Re-verify the reviewed candidate identity (unchanged evidence).
  select * into v_candidate from public.visual_scene_candidate
    where candidate_id = v_hold.candidate_id;
  if not found or v_candidate.tenant_id <> v_hold.tenant_id
      or v_candidate.group_key <> v_hold.group_key
      or v_candidate.phash <> v_hold.candidate_phash
      or v_candidate.exact_url <> v_hold.exact_url
      or v_candidate.fingerprint <> v_hold.fingerprint then
    raise exception 'reviewed scene candidate changed; hold conflict scope drifted'
      using errcode='23514';
  end if;
  if not exists(select 1 from public.visual_scene_phash_occupied o
      where o.phash = v_hold.matched_phash and o.tenant_id = v_hold.matched_tenant_id
        and o.group_key = v_hold.matched_group_key and o.used_date = v_hold.matched_used_date) then
    raise exception 'reviewed occupied scene no longer present; hold conflict scope drifted'
      using errcode='23514';
  end if;
  v_hamming := public.visual_scene_hamming(v_hold.candidate_phash, v_hold.matched_phash);
  if v_hamming is distinct from v_hold.hamming then
    raise exception 'reviewed conflict distance drifted; refusing resolution'
      using errcode='23514';
  end if;
  -- Rescan the LIVE conflict: a fresh scan of the live row must still
  -- surface the exact reviewed matched identity at the exact reviewed
  -- hamming distance, or the review is stale.
  select * into v_scan from public.visual_scene_claim_scan(v_row, v_hold.candidate_id);
  if not exists(select 1 from jsonb_array_elements(coalesce(v_scan.o_detail, '[]'::jsonb)) m
      where m->>'phash' = v_hold.matched_phash
        and m->>'tenant_id' = v_hold.matched_tenant_id
        and m->>'group_key' = v_hold.matched_group_key
        and (m->>'used_date')::date = v_hold.matched_used_date
        and (m->>'hamming')::integer = v_hold.hamming) then
    raise exception 'reviewed conflict no longer present in the live scene scan; refusing stale resolution'
      using errcode='23514';
  end if;
  -- Store the reviewed actor AND evidence one-shot; the row trigger enforces
  -- the single open->terminal transition.
  update public.visual_scene_review_hold
    set state = p_decision, resolved_by = btrim(p_actor), resolved_at = now(),
        resolution_evidence = p_evidence
    where hold_id = p_hold_id;
  return jsonb_build_object('hold_id', p_hold_id, 'state', p_decision,
    'resolved_by', btrim(p_actor),
    'exemption_scope', case when p_decision = 'approved' then jsonb_build_object(
      'tenant_id', v_hold.tenant_id, 'group_key', v_hold.group_key,
      'claim_date', v_hold.claim_date,
      'candidate_id', v_hold.candidate_id,
      'exact_url', v_hold.exact_url,
      'fingerprint', v_hold.fingerprint,
      'candidate_phash', v_hold.candidate_phash,
      'matched_phash', v_hold.matched_phash,
      'matched_tenant_id', v_hold.matched_tenant_id,
      'matched_group_key', v_hold.matched_group_key,
      'matched_used_date', v_hold.matched_used_date) else null end);
end;
$$;

-- ---------------------------------------------------------------------------
-- (P0-7, wave-2 item 3, wave-3 item 3) SAFE REACTIVATION of a held calendar
-- row. Allowed ONLY after the hold is approved AND a fresh bound-candidate
-- re-scan of the row is clean (approved-scope exemptions apply; any new or
-- different conflict leaves the row held). LOCK ORDER (Sol
-- SCENE_WAVE3_SOL_DESIGN.md, superseding the broader scope wording): the
-- CURRENT CALENDAR ROW is locked FOR UPDATE FIRST, then the existing
-- scene-component locks, then the single fleet-wide scene advisory lock —
-- matching actual DML, where a normal UPDATE already holds the row before
-- its trigger seeks component locks. Immediately after the row lock the row
-- is RE-RESOLVED UNCONDITIONALLY (visual_group_resolve_row plus the
-- canonical tenant of its raw calendar key — hint-independent, Sol
-- frozen-hash audit P0); the component locks cover BOTH the re-resolved
-- identity and the reviewed hold identity, and the candidate binding and
-- re-scan run UNDER THE RE-RESOLVED GROUP (never a key persisted on the row
-- or the hold). Cross-tenant rows and rows whose
-- group/date drifted from the reviewed hold are refused. The reactivation
-- UPDATE passes back through the merged calendar trigger chain
-- (content_calendar_visual_group_guard, the ONE authoritative BEFORE path),
-- which re-runs the claim path and records occupancy on the now-clean row —
-- and may legitimately convert the attempted update back to held, so the
-- PERSISTED row is re-read after the UPDATE and the FULL reviewed scope is
-- verified before success: active state with no not-ready reason, canonical
-- tenant, re-resolved group, claim date, and bound candidate identity
-- (candidate_id + exact_url + fingerprint). The UNIQUE-candidate
-- precondition (exactly one binding candidate, equal to the approved hold's)
-- RAISES 23514 before the UPDATE; the persisted-identity postcondition
-- RAISES 23514 after it, so a reactivation can never COMMIT an active row or
-- occupancy whose identity fails verification (Sol frozen-hash audit P0/P1).
-- Non-raising false returns remain ONLY for states that commit nothing
-- dangerous: 'still_blocked' (scan found a live conflict; no mutation made)
-- and 'converted_back_to_held' (the trigger re-held the row — archived,
-- not-ready — so it is not claimable and carries no occupancy). The result
-- jsonb carries the verified persisted facts under 'persisted' on success
-- and on converted_back_to_held. Returns a jsonb outcome; refuses (no
-- mutation) while the row still scans blocked.
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_hold_reactivate(
  p_hold_id uuid, p_actor text
) returns jsonb language plpgsql security definer set search_path = public as $$
declare v_hold public.visual_scene_review_hold%rowtype;
  v_row public.content_calendar%rowtype;
  v_candidate public.visual_scene_candidate%rowtype;
  v_candidate_id uuid; v_scan record; v_updated integer;
  v_row_tenant text; v_resolved text;
begin
  if nullif(btrim(p_actor),'') is null then
    raise exception 'hold reactivation needs a named actor' using errcode='22023';
  end if;
  -- Read the hold identity first (no lock yet) so the calendar row can be
  -- locked FIRST (row -> component -> fleet, wave-3 item 3).
  select * into v_hold from public.visual_scene_review_hold
    where hold_id = p_hold_id;
  if not found then
    raise exception 'scene hold not found' using errcode='P0002';
  end if;
  if v_hold.calendar_row_id is null then
    raise exception 'hold has no calendar row to reactivate; refusing stale reactivation'
      using errcode='23514';
  end if;
  -- 1. Calendar row FOR UPDATE FIRST (matches normal-UPDATE lock order).
  select * into v_row from public.content_calendar
    where id = v_hold.calendar_row_id for update;
  if not found then
    raise exception 'held calendar row not found' using errcode='P0002';
  end if;
  -- RE-RESOLVE the locked current row UNCONDITIONALLY from its current
  -- delivered object (Sol frozen-hash audit, P0): the canonical tenant comes
  -- from the row's raw calendar key, and the group comes from
  -- visual_group_resolve_row — neither the persisted visual_group_key on the
  -- row NOR the group stored on the hold is authority for the checks below.
  v_row_tenant := public.visual_group_tenant_id(v_row.gym_id)::text;
  v_resolved := public.visual_group_resolve_row(v_row);
  -- 2. Scene-component locks for BOTH the re-resolved identity and the
  -- reviewed hold identity, THEN 3. the single fleet-wide advisory lock.
  perform public.visual_group_lock_scene_components(jsonb_build_array(
    jsonb_build_object('gym_id', v_row_tenant, 'group_key', v_resolved),
    jsonb_build_object('gym_id', v_hold.tenant_id, 'group_key', v_hold.group_key)));
  if not pg_try_advisory_xact_lock(hashtextextended(
      jsonb_build_array('visual_scene_global')::text, 0)) then
    raise exception 'global scene claim busy; retry transaction' using errcode='55P03';
  end if;
  -- POST-LOCK STABILITY (Sol independent audit P0, 2026-10-04): re-resolve
  -- under the full lock set and REQUIRE the identity to be unchanged — an
  -- object or alias swapped between the row lock and the component/fleet
  -- locks must never be reactivated on the pre-lock identity.
  if public.visual_group_resolve_row(v_row) is distinct from v_resolved
      or public.visual_group_tenant_id(v_row.gym_id)::text is distinct from v_row_tenant then
    raise exception 'visual identity changed while locking; retry transaction' using errcode='55P03';
  end if;
  -- Re-read the hold under the locks and re-verify its state.
  select * into v_hold from public.visual_scene_review_hold
    where hold_id = p_hold_id for update;
  if not found then
    raise exception 'scene hold not found' using errcode='P0002';
  end if;
  if v_hold.state <> 'approved' then
    raise exception 'only an approved scene hold can reactivate its calendar row'
      using errcode='23514';
  end if;
  -- Cross-tenant and identity-drift guards (wave-3 item 3): the row must
  -- still belong to the reviewed tenant/group/date, where tenant and group
  -- are the RE-RESOLVED identities computed above.
  if v_row_tenant is null or v_row_tenant is distinct from v_hold.tenant_id then
    raise exception 'calendar row moved tenants since the hold; cross-tenant reactivation refused'
      using errcode='23514';
  end if;
  if v_resolved is null or v_resolved is distinct from v_hold.group_key
      or v_row.post_date is distinct from v_hold.claim_date then
    raise exception 'calendar row identity drifted from the reviewed hold; refusing reactivation'
      using errcode='23514';
  end if;
  if not (v_row.status = 'pending' and v_row.variant_status = 'archived'
      and v_row.media_not_ready_reason = 'scene_review_hold') then
    raise exception 'calendar row is not in scene-review-held state' using errcode='23514';
  end if;
  -- Drive the candidate binding and the re-scan from the RE-RESOLVED group.
  v_row.visual_group_key := v_resolved;
  -- UNIQUE-CANDIDATE PRECONDITION (Sol independent audit P1, 2026-10-04,
  -- mirroring visual_scene_hold_resolve): exactly ONE candidate may bind the
  -- live row's delivered object and it must BE the approved hold's reviewed
  -- candidate. Any other shape — none, several, or a different one — is a
  -- fail-closed RAISE (no false return): reactivating on unreviewed
  -- candidate evidence would commit a row and occupancy the reviewer never
  -- approved. (With conflicting pHashes, visual_scene_row_candidate raises
  -- the ambiguous-evidence fail-closed error, which propagates.)
  if (select count(*) from public.visual_scene_candidate c
      join public.visual_scene_row_delivered_object(v_row) d
        on d.object_role = c.object_role and d.exact_url = c.exact_url
      where c.tenant_id = v_hold.tenant_id and c.group_key = v_resolved) <> 1
      or public.visual_scene_row_candidate(v_row) is distinct from v_hold.candidate_id then
    raise exception 'bound scene candidate for the live row is not uniquely the approved candidate; refusing reactivation'
      using errcode='23514';
  end if;
  v_candidate_id := v_hold.candidate_id;
  -- Fresh re-scan of the CURRENT scene state under the locks above.
  select * into v_scan from public.visual_scene_claim_scan(v_row, v_candidate_id);
  if v_scan.o_worst_band is not null then
    return jsonb_build_object('reactivated', false,
      'reason', case v_scan.o_worst_band
        when 'fail_closed' then 'no_bound_candidate'
        else 'still_blocked' end,
      'band', v_scan.o_worst_band, 'matches', v_scan.o_detail,
      'hold_id', p_hold_id);
  end if;
  update public.content_calendar
    set variant_status = 'active', status = 'pending',
        media_not_ready_reason = null
    where id = v_row.id
      and status = 'pending' and variant_status = 'archived'
      and media_not_ready_reason = 'scene_review_hold';
  get diagnostics v_updated = row_count;
  if v_updated <> 1 then
    raise exception 'calendar row mutated during reactivation; refusing reactivation'
      using errcode='23514';
  end if;
  -- Wave-3 item 3 + Sol independent audit P0 (2026-10-04): the UPDATE
  -- re-entered the merged calendar trigger, which re-ran the claim path
  -- (recording occupancy on the now-clean row) and MAY legitimately have
  -- converted the row back to held. Reload the PERSISTED row and verify the
  -- FULL reviewed scope before reporting success: active state with no
  -- not-ready reason, the canonical tenant, the group re-resolved from the
  -- persisted delivered object, the claim date, and the bound candidate
  -- identity (candidate_id + exact_url + fingerprint). Any mismatch is a
  -- failure outcome with the persisted facts attached, never a false
  -- success.
  select * into v_row from public.content_calendar where id = v_row.id;
  v_row_tenant := public.visual_group_tenant_id(v_row.gym_id)::text;
  v_resolved := public.visual_group_resolve_row(v_row);
  v_row.visual_group_key := v_resolved;
  if not (v_row.variant_status = 'active'
      and v_row.media_not_ready_reason is null) then
    return jsonb_build_object('reactivated', false,
      'reason', 'converted_back_to_held', 'hold_id', p_hold_id,
      'calendar_row_id', v_row.id,
      'persisted', jsonb_build_object('status', v_row.status,
        'variant_status', v_row.variant_status,
        'media_not_ready_reason', v_row.media_not_ready_reason,
        'tenant_id', v_row_tenant, 'group_key', v_resolved,
        'post_date', v_row.post_date));
  end if;
  select c.* into v_candidate from public.visual_scene_candidate c
    where c.candidate_id = public.visual_scene_row_candidate(v_row);
  -- RAISING POSTCONDITION (Sol frozen-hash audit P0, 2026-10-04): an
  -- active-but-wrong-identity outcome may NEVER commit. At this point the
  -- row IS active and its occupancy may already be written in this
  -- transaction; a false return would COMMIT both. SQLSTATE 23514 (not
  -- 40001) is deliberate: the merged trigger re-derives identity on the
  -- UPDATE itself, so an active row whose persisted tenant/group/date or
  -- bound candidate fails verification is a CONTRACT violation (something
  -- outside the trigger rewrote or failed to derive identity), not a
  -- transient serialization conflict. The raise rolls back the UPDATE, the
  -- occupancy and any hold-adjacent writes of this transaction.
  if v_row_tenant is distinct from v_hold.tenant_id
      or v_resolved is null or v_resolved is distinct from v_hold.group_key
      or v_row.post_date is distinct from v_hold.claim_date
      or v_candidate.candidate_id is distinct from v_hold.candidate_id
      or v_candidate.exact_url is distinct from v_hold.exact_url
      or v_candidate.fingerprint is distinct from v_hold.fingerprint then
    raise exception 'persisted row identity failed reactivation verification; rolling back reactivation'
      using errcode='23514',
        detail = jsonb_build_object('hold_id', p_hold_id,
          'calendar_row_id', v_row.id,
          'persisted', jsonb_build_object('status', v_row.status,
            'variant_status', v_row.variant_status,
            'media_not_ready_reason', v_row.media_not_ready_reason,
            'tenant_id', v_row_tenant, 'group_key', v_resolved,
            'post_date', v_row.post_date,
            'candidate_id', v_candidate.candidate_id,
            'exact_url', v_candidate.exact_url,
            'fingerprint', v_candidate.fingerprint))::text;
  end if;
  return jsonb_build_object('reactivated', true, 'hold_id', p_hold_id,
    'calendar_row_id', v_row.id, 'reactivated_by', btrim(p_actor),
    'persisted', jsonb_build_object('status', v_row.status,
      'variant_status', v_row.variant_status,
      'media_not_ready_reason', v_row.media_not_ready_reason,
      'tenant_id', v_row_tenant, 'group_key', v_resolved,
      'post_date', v_row.post_date,
      'candidate_id', v_candidate.candidate_id,
      'exact_url', v_candidate.exact_url,
      'fingerprint', v_candidate.fingerprint));
end;
$$;

-- ---------------------------------------------------------------------------
-- (e) Backfill stub. Occupied history must be derived from the exact-byte
-- ledger (visual_global_usage / visual_global_usage_member plus attested
-- candidate phashes) BEFORE any activation. Unknown or source-null published
-- history must STAY HELD for review, and the backfill may never mark a staged
-- candidate as used. That derivation is part of the activation draft and must
-- never run ad hoc; the stub fails closed (ACTIVATION BLOCKER 4).
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_backfill_occupied()
returns integer language plpgsql security definer set search_path = public as $$
begin
  raise exception 'visual_scene_backfill_occupied is an activation-draft deliverable; occupied history must be derived from the exact-byte ledger before activation (0A000 stub)'
    using errcode='0A000';
end;
$$;

-- ---------------------------------------------------------------------------
-- Privileges, matching the existing visual ledger drafts: RLS on, revoke-all,
-- SELECT-only for service_role, guarded RPCs revoked from all non-owner roles
-- except the explicit service_role grants on SAFE read/review entry points.
-- Claim-path SECURITY DEFINER functions (scan/decide/guard/row_candidate/
-- write_holds/calendar trigger/backfill) are trigger/internal-only: revoked
-- from public, anon, authenticated AND service_role (Sol P0-2 / Astra P0).
-- ---------------------------------------------------------------------------
alter table public.visual_scene_candidate enable row level security;
alter table public.visual_scene_phash_occupied enable row level security;
alter table public.visual_scene_review_hold enable row level security;
drop policy if exists visual_scene_candidate_service_role on public.visual_scene_candidate;
create policy visual_scene_candidate_service_role on public.visual_scene_candidate
  for all to service_role using (true) with check (true);
drop policy if exists visual_scene_phash_occupied_service_role on public.visual_scene_phash_occupied;
create policy visual_scene_phash_occupied_service_role on public.visual_scene_phash_occupied
  for all to service_role using (true) with check (true);
drop policy if exists visual_scene_review_hold_service_role on public.visual_scene_review_hold;
create policy visual_scene_review_hold_service_role on public.visual_scene_review_hold
  for all to service_role using (true) with check (true);
revoke all on public.visual_scene_candidate, public.visual_scene_phash_occupied,
  public.visual_scene_review_hold from public, anon, authenticated, service_role;
grant select on public.visual_scene_candidate, public.visual_scene_phash_occupied,
  public.visual_scene_review_hold to service_role;

revoke all on function public.visual_scene_immutable()
  from public, anon, authenticated, service_role;
revoke all on function public.visual_scene_review_hold_mutation()
  from public, anon, authenticated, service_role;
-- Trigger/internal-only claim path (Sol P0-2 / Astra P0).
revoke all on function public.visual_scene_claim_scan(public.content_calendar, uuid)
  from public, anon, authenticated, service_role;
revoke all on function public.visual_scene_claim_decide(public.content_calendar, uuid)
  from public, anon, authenticated, service_role;
revoke all on function public.visual_scene_claim_guard(public.content_calendar, uuid)
  from public, anon, authenticated, service_role;
revoke all on function public.visual_scene_row_candidate(public.content_calendar)
  from public, anon, authenticated, service_role;
revoke all on function public.visual_scene_row_delivered_object(public.content_calendar)
  from public, anon, authenticated, service_role;
revoke all on function public.visual_scene_write_holds(text, text, date, uuid, text, uuid, char(16), text, text, jsonb)
  from public, anon, authenticated, service_role;
revoke all on function public.visual_scene_backfill_occupied()
  from public, anon, authenticated, service_role;
-- Safe read/review entry points (cannot fabricate occupancy or holds).
revoke all on function public.visual_scene_hamming(text, text)
  from public, anon, authenticated;
revoke all on function public.visual_scene_register_candidate(text, text, text, text, text, jsonb, text, text)
  from public, anon, authenticated;
revoke all on function public.visual_scene_hold_resolve(uuid, text, text, jsonb)
  from public, anon, authenticated;
revoke all on function public.visual_scene_hold_reactivate(uuid, text)
  from public, anon, authenticated;
revoke all on function public.visual_scene_publish_claim_guarded(uuid)
  from public, anon, authenticated;
revoke all on function public.visual_scene_approval_guarded(uuid)
  from public, anon, authenticated;
grant execute on function public.visual_scene_hamming(text, text) to service_role;
grant execute on function public.visual_scene_register_candidate(text, text, text, text, text, jsonb, text, text)
  to service_role;
grant execute on function public.visual_scene_hold_resolve(uuid, text, text, jsonb)
  to service_role;
grant execute on function public.visual_scene_hold_reactivate(uuid, text)
  to service_role;
grant execute on function public.visual_scene_publish_claim_guarded(uuid)
  to service_role;
grant execute on function public.visual_scene_approval_guarded(uuid)
  to service_role;

commit;
