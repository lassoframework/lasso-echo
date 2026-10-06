-- DRAFT / UNAPPLIED / OFF — DO NOT APPLY, DO NOT ACTIVATE.
--
-- STATUS: DRAFT / UNAPPLIED / OFF — minimal durable tenant-scoped used-media
-- scene (pHash) claim prevention, ported 2026-10-05 (Child B, scene ledger
-- SQL) from the reviewed claim-wave draft at commit 73b39c2
-- (migrations/DRAFT_visual_scene_claim_wave_20261003.sql, branch
-- codex/echo-phash-global-20261003, draft PR #268). This file supersedes the
-- rejected prep-time sketch DRAFT_visual_scene_phash_20261003.sql, which is
-- intentionally NOT ported (it recorded scenes at preparation time and was
-- never a valid write path). Nothing here arms any guard: no calendar trigger
-- is installed, no flag is flipped, no backfill runs, no historical asset is
-- marked cleared, and NO existing media (approved or otherwise) is
-- quarantined by this file. SCENE_GUARD_OPERATIONAL stays False and
-- AGENT_VISUAL_SCENE_GUARD stays OFF.
--
-- MIGRATION ORDER (exact):
--   1. migrations/DRAFT_visual_group_schema_20261002.sql      (groups, aliases)
--   2. migrations/DRAFT_visual_global_history_20261002.sql    (exact-byte ledger)
--   3. THIS FILE: migrations/DRAFT_visual_scene_ledger_20261005.sql
--   4. (any activation draft — must remain LAST; none exists yet)
-- This file must be applied only after the exact-byte history draft (it
-- references public.visual_group and public.visual_global_object_attestation)
-- and strictly before any activation draft.
--
-- ROLLBACK (before any activation): nothing is applied anywhere; delete the
-- file. If it were ever applied to a scratch database, drop in this order:
--   drop function if exists public.visual_scene_backfill_occupied();
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
-- WHAT THIS FILE IMPLEMENTS (the MINIMAL durable subset of the claim-wave
-- redesign — items (a)-(d); item (e) backfill remains a guarded stub):
--   (a) visual_scene_candidate — prep-time STAGING. A candidate row carries
--       owner-attested pHash evidence bound to exact verified bytes: it must
--       reference a visual_global_object_attestation row (same canonical
--       tenant + group + exact_url + md5 fingerprint), non-empty evidence, a
--       named actor, AND an object_role ('display' or 'poster') binding it to
--       the calendar row's exact DELIVERED object: the displayed image URL,
--       or the video poster/thumbnail URL when that is distinct from the
--       image URL. A bare tenant/group candidate is NEVER enough:
--       visual_scene_row_candidate resolves the candidate for a calendar row
--       by role + exact_url binding and the claim scan rejects (fail closed)
--       any candidate not bound to the row's delivered object. A candidate
--       NEVER counts as use and never gates.
--   (b) visual_scene_phash_occupied — a once-used scene is recorded ONLY by
--       the claim path (visual_scene_claim_decide / visual_scene_claim_guard
--       on a clean verdict), inside the caller's claim transaction. Occupancy
--       is PERMANENT: the append-only immutability trigger blocks
--       UPDATE/DELETE, and no function in this file frees an occupied scene
--       on local release, denial, or swap — mirroring the exact-byte ledger's
--       "released stays consumed" semantics.
--   (c) visual_scene_review_hold — durable, reviewable holds written ONLY by
--       the non-raising claim path (visual_scene_claim_decide). A partial
--       unique index on (tenant_id, group_key, claim_date, calendar_row_id,
--       candidate_id, candidate_phash, matched_phash, matched_tenant_id,
--       matched_group_key, matched_used_date) WHERE state='open' is the
--       stable open-hold uniqueness key: a retry of the same conflict for the
--       same row inserts NO duplicate hold, and a later DISTINCT conflict
--       gets its own hold instead of reusing an approval. Holds carry the
--       full conflict scope; an APPROVED hold exempts ONLY that exact
--       reviewed conflict scope for that exact claim date — later or
--       different conflicts still hold.
--   (d) Server-side hamming comparison over the ENTIRE occupied table inside
--       the claim transaction (visual_scene_hamming is plpgsql over every
--       occupied row; there is deliberately NO exact-match pre-filter — a
--       one-bit-away pHash must be caught). No client-side scan remains a
--       valid decision path.
--   (e) visual_scene_backfill_occupied is a STUB that RAISES 0A000. Occupied
--       history must be derived from the exact-byte ledger before any
--       activation; shipping the backfill is part of the activation draft.
--
-- POLICY (mirrors the claim-wave draft and agent-side classify bands):
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
-- === HOLD DURABILITY CONTRACT ==============================================
-- visual_scene_claim_decide is NON-RAISING for claim outcomes: on a conflict
-- it writes idempotent hold row(s) and returns a decision object, so a caller
-- that COMMITS the blocked claim as a held calendar row keeps the holds; a
-- caller that rolls back keeps nothing. On ANY blocked decision it NEVER
-- writes visual_scene_phash_occupied, so a rejected claim leaves no false
-- "used" mark. visual_scene_claim_guard is the RAISING fail-closed variant
-- for hard-fail integration contexts; because its own raise rolls back all
-- its work, it writes NO hold rows by construction and only records occupancy
-- on a clean verdict. Malformed input (non-canonical scene/date, busy lock)
-- still raises in both — a caller bug or retry signal, not a claim outcome.
-- ===========================================================================
--
-- === EXECUTE REVOCATION =====================================================
-- Following the exact-byte precedent, ALL scene claim-path SECURITY DEFINER
-- functions are revoked from public, anon, authenticated AND service_role:
-- visual_scene_claim_scan, visual_scene_claim_decide,
-- visual_scene_claim_guard, visual_scene_row_candidate,
-- visual_scene_row_delivered_object, visual_scene_write_holds and
-- visual_scene_backfill_occupied. Service callers can never fabricate
-- occupancy or holds outside the calendar transaction. The ONLY
-- service_role-executable scene entry points are the safe read/review
-- surfaces that cannot fabricate occupancy: visual_scene_hamming (pure),
-- visual_scene_register_candidate (prep-time staging; never consumes) and
-- visual_scene_hold_resolve (validated one-shot open->terminal transition).
-- SELECT on the three scene tables stays granted to service_role for review
-- tooling.
-- ===========================================================================
--
-- === FLEET LOCK ORDER =======================================================
-- Occupancy writers and hold writers (decide/guard) use:
--   1. existing scene-component locks (visual_group_lock_scene_components,
--      taken by the caller — the calendar claim path),
--   2. THEN the single fleet-wide scene advisory lock
--      (hashtextextended('["visual_scene_global"]', 0)) inside
--      visual_scene_claim_scan.
-- The scan never takes component locks after the advisory lock. Hold
-- resolution locks in the order a normal UPDATE takes locks: the CURRENT
-- CALENDAR ROW FOR UPDATE FIRST, then the scene-component locks (taken for
-- BOTH the re-resolved identity and the reviewed hold identity), then the
-- single fleet-wide scene advisory lock.
-- ===========================================================================
--
-- === ACTIVATION BLOCKERS (must be resolved by an activation draft) ==========
--  1. CALENDAR BEFORE-PATH WIRING. This file installs NO calendar trigger;
--     the scene decision must be invoked from the ONE authoritative
--     content_calendar BEFORE path (the exact-byte guard
--     visual_group_guard_trigger) by the activation draft, with the ordering
--     invariant: every potentially-raising step completes BEFORE the final
--     idempotent hold insertion (no raise after hold), which commits with the
--     held row.
--  2. PUBLISH-CLAIM / APPROVAL RPC WIRING. The real publish-claim RPC must
--     consult persisted row state (a scene-held row must never mint a token);
--     the approval RPC must exclude held rows. Neither is part of this file.
--  3. ROW REACTIVATION. A post-approval safe-reactivation RPC (re-scan +
--     persisted-identity verification) belongs to the activation draft; it is
--     deliberately NOT ported here.
--  4. BACKFILL (still 0A000). Occupied history must be derived from the
--     exact-byte ledger and attested candidates before activation. Unknown or
--     source-null published history must STAY HELD for review, and the
--     backfill may never mark a staged candidate as used.
--  5. COVERAGE REPORT + LIVE ACCEPTANCE RUNS on a disposable database by an
--     independent reviewer on the exact diff.
-- ===========================================================================
begin;

-- ---------------------------------------------------------------------------
-- (a) Prep-time candidate staging. Never counts as use. object_role binds the
-- candidate to the calendar row's exact DELIVERED object:
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
-- a one-bit-away match); this index serves inspection and hold forensics.
create index if not exists visual_scene_phash_occupied_tenant_date_idx
  on public.visual_scene_phash_occupied (tenant_id, used_date);

-- ---------------------------------------------------------------------------
-- (c) Durable review holds. Written ONLY by the non-raising claim path; a
-- hold is durable IF AND ONLY IF the claim transaction commits in a
-- blocked/held state. The partial unique index is the stable open-hold
-- uniqueness key: it includes the exact candidate (candidate_id +
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

comment on table public.visual_scene_review_hold is
  'DRAFT/UNAPPLIED/OFF: reviewable scene-conflict holds. Written ONLY by the non-raising claim path; durable IF AND ONLY IF the claim transaction commits in a blocked/held state. Stable open-hold uniqueness key (candidate + matched occupied identity) prevents retry duplicates and approval reuse. An approved hold exempts ONLY its exact reviewed candidate + delivered object + matched occupied conflict for its exact claim date. Resolution actor and evidence are stored one-shot (resolved_by / resolution_evidence).';

create index if not exists visual_scene_review_hold_state_idx
  on public.visual_scene_review_hold (state, created_at);
create index if not exists visual_scene_review_hold_scene_idx
  on public.visual_scene_review_hold (tenant_id, group_key, claim_date);
-- Stable open-hold uniqueness: one open hold per exact candidate + exact
-- matched occupied identity per calendar row per claim date. Retries
-- conflict-do-nothing; a distinct later conflict inserts its own hold and can
-- never ride on an earlier approval.
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
-- Row media-kind + exact delivered object. Derives the row's ACTUAL media
-- kind: a non-blank thumbnail_url DISTINCT from image_url means a video row
-- whose DISPLAYED object is the poster ('poster' -> that exact
-- thumbnail_url); anything else is a photo row whose displayed object is the
-- image itself ('display' -> the exact image_url). Returns 0 rows when the
-- row has no displayable object. BOTH the row-candidate binding and the claim
-- scan use this one helper, so display-vs-poster binding can never disagree
-- with the scan and no tenant/group-only match is possible.
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
-- Row-to-candidate binding. Resolves THE staged candidate backing a calendar
-- row by the row's ACTUAL media kind and exact DELIVERED object
-- (visual_scene_row_delivered_object): a video row binds ONLY a 'poster'
-- candidate on its exact thumbnail_url; a photo row binds ONLY a 'display'
-- candidate on its exact image_url. A 'display' candidate on the video file
-- URL never satisfies a video row. The candidate's (tenant, group, exact_url,
-- fingerprint) is already FK-enforced against
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
-- object. Does canonical-scene validation, the single fleet-wide advisory
-- lock (taken AFTER the caller's scene-component locks — see FLEET LOCK
-- ORDER — and never instead of them), the mandatory bound-candidate lookup,
-- and the full occupied-table hamming scan (no exact-match pre-filter).
-- Writes NOTHING. o_worst_band values:
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
  -- Resolve the canonical tenant from the row's raw calendar key.
  -- visual_group_tenant_strict RAISES for an unmapped key, so a raw alias key
  -- resolves here and never has to be pre-canonicalized on the row (the
  -- exact-byte stack deliberately keeps calendar keys raw). All internal
  -- scene keys below use the resolved canonical tenant.
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
  -- Bound candidate: the candidate must belong to THIS scene AND bind to THIS
  -- row's actual media kind + exact delivered object
  -- (visual_scene_row_delivered_object: poster for video rows, display for
  -- photo rows). Unknown or mismatched = fail closed.
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
    -- Approval-scoped exemption: an APPROVED hold exempts ONLY its exact
    -- reviewed scope — claimant tenant/group/date, the exact reviewed
    -- candidate AND delivered object (candidate_id + exact_url + fingerprint
    -- + phash), and the exact matched occupied identity. Later or different
    -- conflicts still hold.
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
-- (c) Shared IDEMPOTENT hold writer. One open hold per exact candidate +
-- exact matched occupied identity per calendar row per claim date; retries
-- insert no duplicate (stable open-hold uniqueness key), and a later DISTINCT
-- conflict inserts its own hold. Returns the hold_ids actually inserted by
-- THIS call (skipped duplicates are not reported again). INTERNAL. Never
-- raises for a claim outcome.
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
-- roles; the calendar claim path is the intended caller). Same locks, same
-- full occupied-table hamming scan, same bands as the raising guard. It NEVER
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
-- input (non-canonical scene/date, busy lock) still raises — that is a caller
-- bug or retry signal, not a claim outcome.
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
        hint = 'raising guard writes no hold by construction; the committed held-row path owns durable holds';
  end if;
  if v_scan.o_worst_band = 'uncertain' then
    raise exception 'uncertain scene match requires review; visual claim held'
      using errcode='23514', detail = v_scan.o_detail::text,
        hint = 'raising guard writes no hold by construction; the committed held-row path owns durable holds';
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
-- HOLD REVIEW PATH. Validated one-shot open->approved|rejected with a
-- non-empty actor and non-empty evidence jsonb (the row trigger enforces the
-- one-shot transition). LOCK ORDER (matches actual DML): the hold identity is
-- read first (unlocked), then the CURRENT CALENDAR ROW is locked FOR UPDATE
-- FIRST, then the scene-component locks, then the single fleet-wide scene
-- advisory lock — a normal UPDATE already holds the row before its trigger
-- seeks component locks, so component/fleet-before-row can deadlock.
-- Immediately after the row lock the row is RE-RESOLVED UNCONDITIONALLY
-- (visual_group_resolve_row plus the canonical tenant of its raw calendar
-- key — hint-independent); the component locks cover BOTH the re-resolved
-- identity and the reviewed hold identity, and the hold is then re-read FOR
-- UPDATE. Before ANY resolution takes effect the LIVE row is rebound and
-- rescanned UNDER THE RE-RESOLVED GROUP (never a key persisted on the row or
-- the hold): the row must still be in the reviewed held state on the reviewed
-- tenant/group/date (stale review and cross-tenant review are refused), the
-- exact delivered-object candidate evidence must still bind to the live row
-- with a valid byte attestation, the reviewed candidate and matched occupied
-- identities must be unchanged, and a fresh visual_scene_claim_scan of the
-- live row must still surface the exact reviewed matched conflict at the
-- exact reviewed hamming distance — any conflict-scope drift rejects the
-- resolution. An approval exempts ONLY the specifically reviewed conflict
-- pair for its exact claim date (enforced in visual_scene_claim_scan) and
-- does NOT activate the row; later or different conflicts still hold. Row
-- reactivation after approval is NOT part of this file (ACTIVATION BLOCKER
-- 3).
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
  -- locked FIRST (row -> component -> fleet).
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
  -- delivered object: the canonical tenant comes from the row's raw calendar
  -- key, and the group comes from visual_group_resolve_row — neither the
  -- persisted visual_group_key on the row NOR the group stored on the hold is
  -- authority for the checks below.
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
  -- POST-LOCK STABILITY: the resolution was computed before the component
  -- locks; the alias evidence behind it is not covered by the calendar row
  -- lock. Re-resolve under the full lock set and REQUIRE the identity to be
  -- unchanged — an object or alias swapped between the row lock and the
  -- component/fleet locks must never be approved on the pre-lock identity.
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
  -- delivered object must still be the reviewed group, and the row must still
  -- sit in the exact reviewed held state on the reviewed claim date.
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
  -- BOUND-CANDIDATE IDENTITY: the reviewed candidate must be the UNIQUE
  -- candidate bound to the live row's delivered object. If a second candidate
  -- now binds the same object — even with identical evidence — the binding
  -- set differs from what was reviewed, and (with conflicting pHashes)
  -- visual_scene_row_candidate raises the ambiguous-evidence fail-closed
  -- error, which propagates. Approving would exempt a conflict the reviewer
  -- never saw; refuse.
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
  -- Rescan the LIVE conflict: a fresh scan of the live row must still surface
  -- the exact reviewed matched identity at the exact reviewed hamming
  -- distance, or the review is stale.
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
-- row_delivered_object/write_holds/backfill) are trigger/internal-only:
-- revoked from public, anon, authenticated AND service_role.
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
-- Trigger/internal-only claim path.
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
grant execute on function public.visual_scene_hamming(text, text) to service_role;
grant execute on function public.visual_scene_register_candidate(text, text, text, text, text, jsonb, text, text)
  to service_role;
grant execute on function public.visual_scene_hold_resolve(uuid, text, text, jsonb)
  to service_role;

commit;
