-- DRAFT / UNAPPLIED / OFF — DO NOT APPLY, DO NOT ACTIVATE.
--
-- STATUS: DRAFT / UNAPPLIED / OFF — claim-wave redesign, 2026-10-03;
-- P0 repair pass (Sol/Astra SCENE_AUDIT_GAPS.md), 2026-10-04. Still OFF: no
-- production apply, no flag activation. The calendar trigger installed here is
-- a DRAFT on the same OFF branch; SCENE_GUARD_OPERATIONAL stays False and
-- AGENT_VISUAL_SCENE_GUARD stays OFF.
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
--   drop trigger if exists content_calendar_scene_wave_claim_guard on public.content_calendar;
--   drop function if exists public.visual_scene_calendar_claim_guard();
--   drop function if exists public.visual_scene_backfill_occupied();
--   drop function if exists public.visual_scene_approval_guarded(uuid);
--   drop function if exists public.visual_scene_publish_claim_guarded(uuid);
--   drop function if exists public.visual_scene_hold_reactivate(uuid,text);
--   drop function if exists public.visual_scene_hold_resolve(uuid,text,text,jsonb);
--   drop function if exists public.visual_scene_claim_decide(public.content_calendar,uuid);
--   drop function if exists public.visual_scene_claim_guard(public.content_calendar,uuid);
--   drop function if exists public.visual_scene_claim_scan(public.content_calendar,uuid);
--   drop function if exists public.visual_scene_row_candidate(public.content_calendar);
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
--       the claim path (the draft BEFORE trigger, or the internal
--       decide/guard functions on a clean verdict), inside the same
--       transaction as the local/exact-byte sync. Occupancy is PERMANENT:
--       the append-only immutability trigger blocks UPDATE/DELETE, and no
--       function in this file frees an occupied scene on local release,
--       denial, or swap — mirroring the exact-byte ledger's "released stays
--       consumed" semantics (audit P1-3 / repair-spec occupancy persistence).
--   (c) visual_scene_review_hold — durable, reviewable holds written ONLY by
--       the non-raising claim paths (decide / the draft BEFORE trigger). A
--       partial unique index on
--       (tenant_id, group_key, claim_date, calendar_row_id, candidate_phash,
--        matched_phash) WHERE state='open' is the stable open-hold uniqueness
--       key: a retry of the same conflict for the same row inserts NO
--       duplicate hold (Sol P0-3). Holds carry the full conflict scope
--       (claimant tenant/group/date/phash + matched tenant/group/date/phash);
--       an APPROVED hold exempts ONLY that exact reviewed conflict pair for
--       that exact claim date — later or different conflicts still hold.
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
-- === HOLD-ROLLBACK: ENFORCED SEMANTICS (realized) ============================
-- A hold row exists IF AND ONLY IF the claim transaction commits in a
-- blocked/held state. This is now REALIZED by the committed held calendar row:
-- the draft BEFORE trigger (visual_scene_calendar_claim_guard) NEVER raises on
-- a scene conflict — it writes the idempotent hold evidence, mutates NEW to
-- the held state (variant_status='archived', status='pending',
-- media_not_ready_reason='scene_review_hold', publish_claim_token cleared),
-- still passes NEW through visual_group_sync_row so any old active local
-- membership is released, and RETURNS NEW so the transaction COMMITS the held
-- row with its holds. No exact claim and no pHash occupancy are created for
-- the held row. Because the conflict path never raises, the holds it wrote
-- commit with the row — no caller replay, no errdetail contract.
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
-- visual_scene_write_holds, visual_scene_calendar_claim_guard, and
-- visual_scene_backfill_occupied. Service callers can never fabricate
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
-- Every occupancy writer, hold writer, hold resolution and the future
-- backfill uses the SAME order:
--   1. existing scene-component locks (visual_group_lock_scene_components,
--      already taken by visual_group_guard_trigger / visual_group_sync_row),
--   2. THEN the single fleet-wide scene advisory lock
--      (hashtextextended('["visual_scene_global"]', 0)) inside
--      visual_scene_claim_scan.
-- The scan never takes component locks after the advisory lock; the trigger
-- runs sync_row (which takes component locks) BEFORE the scan. Hold
-- resolution touches only scene tables, so it takes the advisory lock alone.
-- ===========================================================================
--
-- === ACTIVATION BLOCKERS (must be resolved by the activation draft) ==========
--  1. CALENDAR WIRING / RESOLVED-KEY ORDER. The draft trigger
--     content_calendar_scene_wave_claim_guard fires BEFORE
--     content_calendar_visual_group_guard (alphabetical) so a held row never
--     reaches the exact-byte claim. It therefore sees new.visual_group_key
--     BEFORE visual_group_guard_trigger resolves aliases, and engages only
--     when the row already carries a resolved visual_group_key + post_date
--     (armed tenant, active unsent row). The ACTIVATION draft MUST move the
--     scene decision inside visual_group_guard_trigger after visual_group_key
--     resolution (or add the post-resolution hook there), without ever
--     creating an exact claim or occupancy for a row that ends held.
--  2. PUBLISH-CLAIM RPC WIRING. The real publish-claim RPC (PR230 path) is
--     not part of this draft stack; visual_scene_publish_claim_guarded
--     enforces the persisted-state contract (NULL for held rows) and raises
--     0A000 for the actual token mint. The activation draft must route the
--     real RPC through this persisted-state check. The approval RPC must
--     likewise consult persisted state (visual_scene_approval_guarded).
--  3. PUBLISH RESERVATION COLUMNS. In this draft stack the only publish
--     reservation marker on content_calendar is publish_claim_token (plus
--     late_post_id as provider id); no separate publish reservation column
--     exists. The held mutation clears publish_claim_token. If production
--     content_calendar has additional reservation columns, the activation
--     draft must name and clear them explicitly — they are integration
--     prerequisites, not invented here.
--  4. BACKFILL (still 0A000). Occupied history must be derived from the
--     exact-byte ledger and attested candidates before activation. Unknown or
--     source-null published history must STAY HELD for review, and the
--     backfill may never mark a staged candidate as used.
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
-- header). The partial unique index is the stable open-hold uniqueness key:
-- retrying the same conflict for the same calendar row cannot duplicate an
-- open hold. Identity columns double as the conflict scope for
-- approval-scoped exemption. Resolution columns may be set exactly once, from
-- 'open' to a terminal resolution, by the validated review RPC.
-- ---------------------------------------------------------------------------
create table if not exists public.visual_scene_review_hold (
  hold_id          uuid        primary key default gen_random_uuid(),
  tenant_id        text        not null,
  group_key        text        not null,
  claim_date       date        not null,
  calendar_row_id  uuid,
  channel          text,
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
  created_at       timestamptz not null default now(),
  check ((state = 'open') = (resolved_by is null and resolved_at is null))
);

comment on table public.visual_scene_review_hold is
  'DRAFT/UNAPPLIED/OFF: reviewable scene-conflict holds. Written ONLY by the non-raising claim paths; durable IF AND ONLY IF the claim transaction commits in a blocked/held state (committed held calendar row). Stable open-hold uniqueness key prevents retry duplicates. An approved hold exempts ONLY its exact reviewed conflict pair for its exact claim date.';

create index if not exists visual_scene_review_hold_state_idx
  on public.visual_scene_review_hold (state, created_at);
create index if not exists visual_scene_review_hold_scene_idx
  on public.visual_scene_review_hold (tenant_id, group_key, claim_date);
-- Stable open-hold uniqueness (Sol P0-3): one open hold per exact conflict
-- pair per calendar row per claim date. Retries conflict-do-nothing.
create unique index if not exists visual_scene_review_hold_open_uq
  on public.visual_scene_review_hold
  (tenant_id, group_key, claim_date, calendar_row_id, candidate_phash, matched_phash)
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
      or new.resolved_at is null then
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
-- (a)+(P0-1) Row-to-candidate binding. Resolves THE staged candidate backing
-- a calendar row by the row's exact DELIVERED object: object_role 'display'
-- must equal the row's displayed image URL; object_role 'poster' must equal
-- the row's video poster/thumbnail URL, only when that is distinct from the
-- image URL. The candidate's (tenant, group, exact_url, fingerprint) is
-- already FK-enforced against visual_global_object_attestation. Returns NULL
-- when no candidate is bound; raises fail-closed if MORE THAN ONE distinct
-- pHash is staged against the same delivered object (ambiguous evidence).
-- Trigger/internal only (EXECUTE revoked from all non-owner roles).
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_row_candidate(
  p_row public.content_calendar
) returns uuid language plpgsql stable security definer set search_path = public as $$
declare v_tenant text; v_ids uuid[]; v_phashes text[];
begin
  v_tenant := public.visual_group_tenant_id(p_row.gym_id)::text;
  if v_tenant is null or p_row.visual_group_key is null then
    return null;
  end if;
  select array_agg(candidate_id order by candidate_id),
    array_agg(distinct phash)
    into v_ids, v_phashes
    from public.visual_scene_candidate c
    where c.tenant_id = v_tenant and c.group_key = p_row.visual_group_key
      and ((c.object_role = 'display'
              and nullif(btrim(p_row.image_url),'') is not null
              and c.exact_url = p_row.image_url)
        or (c.object_role = 'poster'
              and nullif(btrim(p_row.thumbnail_url),'') is not null
              and p_row.thumbnail_url is distinct from p_row.image_url
              and c.exact_url = p_row.thumbnail_url));
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
  o_tenant := public.visual_group_tenant_strict(p_row.gym_id)::text;
  if p_row.gym_id is distinct from o_tenant or nullif(btrim(p_row.visual_group_key),'') is null
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
  -- Bound candidate (Sol P0-1): the candidate must belong to THIS scene AND
  -- bind to THIS row's exact delivered object (role + exact_url +
  -- fingerprint, attested). Unknown or mismatched = fail closed.
  select * into v_candidate from public.visual_scene_candidate
    where candidate_id = p_candidate_id
      and tenant_id = o_tenant and group_key = p_row.visual_group_key
      and ((object_role = 'display'
              and nullif(btrim(p_row.image_url),'') is not null
              and exact_url = p_row.image_url)
        or (object_role = 'poster'
              and nullif(btrim(p_row.thumbnail_url),'') is not null
              and p_row.thumbnail_url is distinct from p_row.image_url
              and exact_url = p_row.thumbnail_url));
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
    -- Approval-scoped exemption: an APPROVED hold exempts ONLY its exact
    -- reviewed conflict pair (claimant tenant/group/candidate phash vs this
    -- matched occupied row) for its exact claim date. Later or different
    -- conflicts still hold.
    if exists(select 1 from public.visual_scene_review_hold h
        where h.state = 'approved'
          and h.tenant_id = o_tenant and h.group_key = p_row.visual_group_key
          and h.claim_date = p_row.post_date
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
-- (c) Shared IDEMPOTENT hold writer (Sol P0-3). One open hold per exact
-- conflict pair per calendar row per claim date; retries insert no duplicate
-- (stable open-hold uniqueness key). Returns the hold_ids actually inserted
-- by THIS call (skipped duplicates are not reported again). INTERNAL.
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
       candidate_phash, matched_phash, hamming, matched_tenant_id,
       matched_group_key, matched_used_date, hold_kind, evidence)
      values (p_tenant, p_group_key, p_claim_date, p_row_id, p_channel,
        p_candidate_phash, v_match.phash, v_match.dist, v_match.tenant_id,
        v_match.group_key, v_match.used_date, v_match.band,
        jsonb_build_object('candidate_id', p_candidate_id,
          'fingerprint', p_fingerprint, 'exact_url', p_exact_url))
      on conflict (tenant_id, group_key, claim_date, calendar_row_id,
                   candidate_phash, matched_phash) where state = 'open'
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
-- (P0-3/P0-4) DRAFT calendar BEFORE-trigger: the committed held-row contract.
-- Fires BEFORE content_calendar_visual_group_guard (alphabetical trigger
-- name) so a row that ends held NEVER reaches the local/exact-byte claim as
-- claimable. Engages only for armed tenants (visual_group_enforcement_on),
-- active unsent rows that already carry a resolved visual_group_key and
-- post_date (see ACTIVATION BLOCKER 1: resolved-key ordering). Scenes with
-- no staged candidates are inert (feature OFF for that scene); a scene WITH
-- staged candidates is fail-closed for rows lacking a bound candidate.
--
-- DECISION-FIRST ordering: the scene scan runs BEFORE any local/exact-byte
-- claim, so a conflicting row NEVER receives an exact claim or pHash
-- occupancy. Fleet lock order is preserved by taking the existing
-- scene-component locks explicitly (visual_group_lock_scene_components, the
-- same helper the group guard uses) BEFORE the scan takes the single global
-- scene advisory lock.
--
-- CONFLICT path (never raises): write idempotent hold evidence, mutate NEW to
-- the held state (variant_status='archived', status='pending',
-- media_not_ready_reason='scene_review_hold', publish_claim_token cleared),
-- STILL pass NEW through visual_group_sync_row so the old active local
-- membership is released (the held NEW is not claimable, so sync creates NO
-- exact claim and NO sibling for it), and RETURN NEW so the transaction
-- COMMITS the held row with its holds.
--
-- CLEAN path: run the existing local/exact-byte sync FIRST
-- (visual_group_sync_row — which may hard-reject on missing/invalid byte
-- attestation), THEN record pHash occupancy under the still-held fleet-wide
-- advisory lock, and RETURN NEW. The downstream group guard re-runs its own
-- logic and sync idempotently.
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_calendar_claim_guard()
returns trigger language plpgsql security definer set search_path = public as $$
declare v_tenant text; v_candidate_id uuid; v_scan record;
begin
  if tg_op = 'DELETE' then
    return old;
  end if;
  v_tenant := public.visual_group_tenant_id(new.gym_id)::text;
  if v_tenant is null or not public.visual_group_enforcement_on(new.gym_id) then
    return new;
  end if;
  -- Engage only on active, unsent, already-keyed staging rows. Publish /
  -- finalize / ambiguous flows are guarded elsewhere (group guard's
  -- not-ready raise + visual_scene_publish_claim_guarded).
  if not public.visual_group_row_active(new)
      or public.visual_group_row_ambiguous(new)
      or new.visual_group_key is null or new.post_date is null then
    return new;
  end if;
  -- Scenes with no staged candidates are inert (scene guard OFF for them).
  if not exists(select 1 from public.visual_scene_candidate c
      where c.tenant_id = v_tenant and c.group_key = new.visual_group_key) then
    return new;
  end if;
  -- Scene guard engaged. Missing binding to the row's delivered object is a
  -- HARD rejection (fail closed), not a silent pass.
  v_candidate_id := public.visual_scene_row_candidate(new);
  if v_candidate_id is null then
    raise exception 'no scene candidate bound to this row''s delivered object; scene guard fails closed'
      using errcode='23514';
  end if;
  -- Fleet lock order: existing scene-component locks FIRST (same helper and
  -- closure shape the group guard uses), THEN the single global scene
  -- advisory lock inside the scan.
  perform public.visual_group_lock_scene_components(jsonb_build_array(
    jsonb_build_object('gym_id', v_tenant, 'group_key', new.visual_group_key),
    jsonb_build_object('gym_id', case when tg_op = 'UPDATE'
      then public.visual_group_tenant_id(old.gym_id)::text end,
      'group_key', case when tg_op = 'UPDATE' then old.visual_group_key end)));
  select * into v_scan from public.visual_scene_claim_scan(new, v_candidate_id);
  if v_scan.o_worst_band is not null and v_scan.o_worst_band <> 'fail_closed' then
    -- CONFLICT: idempotent holds + committed held row. NEVER raise here: an
    -- exception after hold insertion would erase the holds and fail the
    -- committed-held-row contract.
    perform public.visual_scene_write_holds(v_scan.o_tenant,
      new.visual_group_key, new.post_date, new.id, new.account,
      v_scan.o_candidate_id, v_scan.o_phash, v_scan.o_fingerprint,
      v_scan.o_exact_url, v_scan.o_detail);
    new.variant_status := 'archived';
    new.status := 'pending';
    new.media_not_ready_reason := 'scene_review_hold';
    new.publish_claim_token := null;
    -- Release any old active local membership for the HELD replacement. The
    -- held NEW is not claimable, so sync creates NO exact claim and NO pHash
    -- occupancy for it.
    perform public.visual_group_sync_row(
      case when tg_op = 'UPDATE' then old end, new, lower(tg_op));
    return new;
  end if;
  if v_scan.o_worst_band = 'fail_closed' then
    -- Candidate evidence vanished between binding and scan: fail closed.
    raise exception 'no scene candidate bound to this row''s delivered object; scene guard fails closed'
      using errcode='23514';
  end if;
  -- CLEAN: existing local/exact-byte sync FIRST (hard-rejects on missing or
  -- invalid byte attestation), THEN pHash occupancy under the still-held
  -- fleet-wide advisory lock, before returning.
  perform public.visual_group_sync_row(
    case when tg_op = 'UPDATE' then old end, new, lower(tg_op));
  insert into public.visual_scene_phash_occupied
    (phash, tenant_id, group_key, used_date, fingerprint,
     calendar_row_id, channel, evidence)
    values (v_scan.o_phash, v_scan.o_tenant, new.visual_group_key, new.post_date,
      v_scan.o_fingerprint, new.id, new.account,
      jsonb_build_object('candidate_id', v_scan.o_candidate_id,
        'exact_url', v_scan.o_exact_url))
    on conflict (phash, tenant_id, group_key, used_date) do nothing;
  return new;
end;
$$;
drop trigger if exists content_calendar_scene_wave_claim_guard on public.content_calendar;
-- Alphabetical ordering puts this BEFORE content_calendar_visual_group_guard.
create trigger content_calendar_scene_wave_claim_guard before insert or update
  on public.content_calendar for each row
  execute function public.visual_scene_calendar_claim_guard();

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
-- (P0-7) HOLD REVIEW PATH. Validated one-shot open->approved|rejected with a
-- non-empty actor and non-empty evidence jsonb (the row trigger enforces the
-- one-shot transition). Before ANY approval takes effect the CURRENT scene
-- state is re-evaluated under the fleet-wide scene advisory lock: the
-- reviewed candidate and matched occupied row must still exist and still sit
-- at the recorded hamming distance, so an approval can never exempt a drifted
-- conflict. An approval exempts ONLY the specifically reviewed conflict pair
-- for its exact claim date (enforced in visual_scene_claim_scan); later or
-- different conflicts still hold.
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_hold_resolve(
  p_hold_id uuid, p_decision text, p_actor text, p_evidence jsonb
) returns jsonb language plpgsql security definer set search_path = public as $$
declare v_hold public.visual_scene_review_hold%rowtype;
  v_candidate public.visual_scene_candidate%rowtype;
  v_hamming integer;
begin
  if p_decision is null or p_decision not in ('approved','rejected')
      or nullif(btrim(p_actor),'') is null
      or p_evidence is null or jsonb_typeof(p_evidence) <> 'object'
      or p_evidence = '{}'::jsonb then
    raise exception 'hold resolution needs decision approved|rejected, a named actor and evidence'
      using errcode='22023';
  end if;
  -- Resolution touches only scene tables, so the fleet-wide scene advisory
  -- lock alone is the complete lock order here.
  if not pg_try_advisory_xact_lock(hashtextextended(
      jsonb_build_array('visual_scene_global')::text, 0)) then
    raise exception 'global scene claim busy; retry transaction' using errcode='55P03';
  end if;
  select * into v_hold from public.visual_scene_review_hold
    where hold_id = p_hold_id for update;
  if not found then
    raise exception 'scene hold not found' using errcode='P0002';
  end if;
  if v_hold.state <> 'open' then
    raise exception 'scene hold is already resolved' using errcode='23514';
  end if;
  -- Re-evaluate the CURRENT scene state before any resolution takes effect.
  select * into v_candidate from public.visual_scene_candidate
    where candidate_id = (v_hold.evidence->>'candidate_id')::uuid;
  if not found or v_candidate.phash <> v_hold.candidate_phash then
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
  update public.visual_scene_review_hold
    set state = p_decision, resolved_by = btrim(p_actor), resolved_at = now()
    where hold_id = p_hold_id;
  return jsonb_build_object('hold_id', p_hold_id, 'state', p_decision,
    'resolved_by', btrim(p_actor),
    'exemption_scope', case when p_decision = 'approved' then jsonb_build_object(
      'tenant_id', v_hold.tenant_id, 'group_key', v_hold.group_key,
      'claim_date', v_hold.claim_date, 'candidate_phash', v_hold.candidate_phash,
      'matched_phash', v_hold.matched_phash,
      'matched_tenant_id', v_hold.matched_tenant_id,
      'matched_group_key', v_hold.matched_group_key,
      'matched_used_date', v_hold.matched_used_date) else null end);
end;
$$;

-- ---------------------------------------------------------------------------
-- (P0-7) SAFE REACTIVATION of a held calendar row. Allowed ONLY after the
-- hold is approved AND a fresh bound-candidate re-scan of the row is clean
-- (approved-pair exemptions apply; any new or different conflict leaves the
-- row held). The reactivation UPDATE passes back through the calendar
-- trigger chain, which re-runs the claim path and records occupancy on the
-- now-clean row. Returns a jsonb outcome; refuses (no mutation) while the
-- row still scans blocked.
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_hold_reactivate(
  p_hold_id uuid, p_actor text
) returns jsonb language plpgsql security definer set search_path = public as $$
declare v_hold public.visual_scene_review_hold%rowtype;
  v_row public.content_calendar%rowtype;
  v_candidate_id uuid; v_scan record;
begin
  if nullif(btrim(p_actor),'') is null then
    raise exception 'hold reactivation needs a named actor' using errcode='22023';
  end if;
  if not pg_try_advisory_xact_lock(hashtextextended(
      jsonb_build_array('visual_scene_global')::text, 0)) then
    raise exception 'global scene claim busy; retry transaction' using errcode='55P03';
  end if;
  select * into v_hold from public.visual_scene_review_hold
    where hold_id = p_hold_id;
  if not found then
    raise exception 'scene hold not found' using errcode='P0002';
  end if;
  if v_hold.state <> 'approved' then
    raise exception 'only an approved scene hold can reactivate its calendar row'
      using errcode='23514';
  end if;
  select * into v_row from public.content_calendar where id = v_hold.calendar_row_id;
  if not found then
    raise exception 'held calendar row not found' using errcode='P0002';
  end if;
  if not (v_row.status = 'pending' and v_row.variant_status = 'archived'
      and v_row.media_not_ready_reason = 'scene_review_hold') then
    raise exception 'calendar row is not in scene-review-held state' using errcode='23514';
  end if;
  v_candidate_id := public.visual_scene_row_candidate(v_row);
  if v_candidate_id is null then
    return jsonb_build_object('reactivated', false,
      'reason', 'no_bound_candidate', 'hold_id', p_hold_id);
  end if;
  -- Fresh re-scan of the CURRENT scene state. Component locks first (the
  -- reactivation UPDATE's trigger chain retakes them in order), then the
  -- global scene advisory lock is already held above.
  perform public.visual_group_lock_scene_components(jsonb_build_array(
    jsonb_build_object('gym_id', v_row.gym_id, 'group_key', v_row.visual_group_key)));
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
  return jsonb_build_object('reactivated', true, 'hold_id', p_hold_id,
    'calendar_row_id', v_row.id, 'reactivated_by', btrim(p_actor));
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
revoke all on function public.visual_scene_write_holds(text, text, date, uuid, text, uuid, char(16), text, text, jsonb)
  from public, anon, authenticated, service_role;
revoke all on function public.visual_scene_calendar_claim_guard()
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
