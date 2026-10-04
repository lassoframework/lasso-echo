-- ============================================================================
-- DRAFT / UNAPPLIED / OFF — visual scene ORIGINAL-USE RECEIPT (two-phase,
-- round-2 repairs)
-- Date: 2026-10-04. Owner: codex/echo-scene-original-use-receipt-20261004.
--
-- ROUND-1 REDESIGN (after the independent Sol safety review,
-- /tmp/fixer_scene_receipt_sol_safety_20261004.log): never mint in a raising
-- terminal AFTER trigger after the provider send; one receipt per SHARED
-- ledger event, never one per sibling; calendar published status is never
-- provider-use proof. Two phases: pre-send prepare (immutable PREPARED
-- attempt bound to the exact live claim token + byte/read/render/
-- reservation evidence) and a separate idempotent terminal RPC.
--
-- ROUND-2 REPAIRS (2026-10-04, second independent rejection of the draft):
--   P0: BEFORE UPDATE OR DELETE OR TRUNCATE ... FOR EACH ROW is invalid
--       PostgreSQL — TRUNCATE triggers are statement-level only, so the
--       draft could not install at all. Every append-only guard is now a
--       FOR EACH ROW UPDATE/DELETE trigger PLUS a separate FOR EACH
--       STATEMENT TRUNCATE trigger.
--   P1: service_role could call terminate with an arbitrary 'delivered'
--       outcome, any provider_post_id and optional {} evidence and mint a
--       FALSE receipt. Terminal outcomes now require a matching row in
--       public.visual_scene_original_use_attestation: an owner-only,
--       append-only, RLS-protected AUTHORITATIVE provider outcome
--       attestation bound to the exact claim token (FK), tenant, calendar
--       row, provider/account/channel/post id and the exact delivered byte
--       lineage, with non-empty readback evidence. service_role has no
--       INSERT/UPDATE/DELETE on it, so the public RPC cannot be used to
--       forge a delivery. NO attester role/runtime exists yet: until that
--       separately reviewed integration writes attestations, terminal
--       delivery is IMPOSSIBLE (fail-closed). This package deliberately
--       does not invent provider proof.
--   P1: the raw calendar gym alias key was used for ledger lookup/store
--       while the exact claim flow canonicalizes (visual_group_tenant_id at
--       the exact-byte trigger boundary; visual_group_tenant_strict in the
--       scene scan) and keys visual_group/ledger/sibling/occupancy by the
--       CANONICAL tenant. prepare now resolves the canonical tenant with
--       visual_group_tenant_strict and uses it for every internal key,
--       including the stored ledger_gym_id (FK-bound to the ledger).
--   P1: caller-supplied source/delivered URLs could differ from the row
--       and from occupancy evidence.exact_url while the md5 matched.
--       prepare now requires delivered_url to be the row's ACTUAL current
--       delivered object (visual_scene_row_delivered_object: poster for
--       video rows, display for photo rows) AND the current occupancy
--       row's evidence->>'exact_url', and binds source_url to the row's
--       source_media_url — or, when the row records no distinct source
--       media, requires source == delivered (no render lineage without a
--       row-recorded source object).
--   P1: same-date same-image siblings share ONE occupancy PK
--       (phash, tenant_id, group_key, used_date), so binding prepare to
--       occupancy.calendar_row_id made the second sibling unable to
--       prepare. prepare now accepts the SHARED occupancy row for the
--       ledger reservation holder OR any ACTIVE same-day sibling of the
--       same ledger event. One occupancy row, one ledger event, one
--       receipt; each sibling still gets its own attempt/token.
--   P1 (test scope): the old provider-failure scenario proved only RPC
--       rollback/retry; it is renamed and re-scoped. Provider send
--       behavior, authoritative readback and application-level no-resend
--       remain UNPROVEN integration properties (see
--       docs/SCENE_ORIGINAL_USE_RECEIPT.md).
--
-- ROUND-3 REPAIRS (2026-10-04, third independent review — two P1
--   integration defects that static 11/11 and scratch-PG 30/30 missed
--   because every prior scenario ran with enforcement UNARMED):
--   P1: terminate marked the row published WITHOUT the
--       current-transaction visual_group_reconciliation receipt the ARMED
--       claim trigger demands for an ambiguous row's finalization
--       (DRAFT_visual_group_claim_trigger_20261002.sql,
--       visual_group_finalization_requires_evidence /
--       visual_group_finalization_evidenced — 'ambiguous publication
--       requires terminal provider reconciliation'). The first terminal
--       publication could never succeed on an armed tenant. terminate now
--       writes that receipt in-band AFTER the owner-only attestation
--       gate, from validated attempt/attestation data only, for BOTH
--       terminal outcomes ('confirmed_published' /
--       'confirmed_not_sent'). service_role has no INSERT on
--       visual_group_reconciliation, so the receipt cannot be forged
--       through the public RPC: a forged/missing attestation means no
--       reconciliation row and the armed trigger keeps refusing. The
--       existing trigger is NOT weakened.
--   P1: prepare required ledger.state='reserved', but the FIRST
--       sibling's terminal publication moves the SHARED ledger row to
--       'published' (the armed claim trigger does this on finalization,
--       preserving reserved_date), so a lawful later same-day channel
--       sibling could never prepare. prepare now accepts a 'published'
--       ledger on THIS exact reserved_date; released, other-date and
--       missing ledgers still refuse. Tenant/date/bytes/occupancy
--       binding is unchanged.
--   P2: the expected provider and channel account are now bound
--       immutably at prepare (expected_provider = the caller-declared
--       send provider; expected_channel = the row's account — a row
--       with no account refuses) and the terminal attestation must
--       match BOTH exactly.
--
-- ROUND-4 REPAIRS (2026-10-04, fourth independent review — one P1 and one
--   P2 rejected on the round-3 draft):
--   P1: terminate copied ALL visual_group_usage_sibling attempts and ALL
--       review_hold event ids for the calendar row into
--       visual_group_reconciliation, so unrelated HISTORICAL ambiguity
--       appeared resolved merely by inclusion. Fail-closed now:
--       visual_scene_original_use_check_ambiguity refuses (a) any
--       UNRECONCILED ambiguous sibling of the row whose original claim
--       token is NULL/older/different than the exact live token, whose
--       original image is NULL or differs from the current delivered
--       object, or whose original provider post id is set and differs
--       from the current attested post (ANY set post refuses pre-send,
--       where no current post exists); and (b) any UNCOVERED review_hold
--       for the row without per-claim evidence proving it current
--       (non-JSON reason, missing/mismatched publish_claim_token,
--       missing/mismatched image_url, mismatched late_post_id or
--       post_date). prepare runs the check BEFORE the attempt row exists
--       (no attempt => no send); terminate re-runs it AFTER the attempt
--       and row locks, so ambiguity inserted or changed between prepare
--       and terminate cannot be laundered — the terminal transaction
--       raises and commits NO receipt, NO reconciliation, NO publication
--       and NO historical clearance. Reconciliation attempts/reserved
--       groups/hold ids are built ONLY from validated current
--       siblings/holds. A current ambiguous sibling with the exact
--       current token and the exact current image may still be covered
--       (armed valid path preserved). Unresolvable context leaves the
--       row HELD for manual evidence recovery. The armed claim trigger
--       is NOT weakened and no coverage is manufactured.
--   P2: provider_account_id was attester-asserted but unbound. prepare
--       now REQUIRES a non-empty expected_provider_account_id (caller's
--       declared destination account from a verified provider
--       connection — declared, NOT inherently trusted), stores it
--       immutably on the attempt (guard-frozen), and the terminal
--       attestation's provider_account_id must match it EXACTLY.
--
-- This file is a proof package only: it creates no trigger on
-- content_calendar, flips no flag, wires no app code, and touches no
-- GoHighLevel anything. Apply order: strictly AFTER
-- DRAFT_visual_scene_ledger_coverage_gate_20261004.sql.
-- ============================================================================

begin;

-- Fail-closed late install: once any tenant is armed, history already exists
-- that this evidence class never covered; installing now would silently
-- change what activation means. Refuse.
do $$
begin
  if exists (select 1 from public.gym_visual_guard_settings where enforce) then
    raise exception 'visual_scene_original_use_receipt must be installed '
      'before any tenant is armed' using errcode = '23514';
  end if;
end $$;

-- ----------------------------------------------------------------------------
-- Phase-1 table: prepared attempts (immutable evidence snapshot per token).
-- ----------------------------------------------------------------------------
create table if not exists public.visual_scene_original_use_attempt (
  attempt_id            uuid primary key default gen_random_uuid(),
  -- Exact claim token binding: equals content_calendar.publish_claim_token
  -- at prepare time (verified in-band). One prepared attempt per token.
  claim_attempt_id      uuid not null unique,
  tenant_id             text not null,           -- canonical tenant (derived)
  ledger_gym_id         text not null,           -- CANONICAL ledger PK part,
                                                 -- exactly as the claim flow
                                                 -- keys the ledger
  group_key             text not null,
  used_date             date not null,
  calendar_row_id       uuid not null,           -- evidence pointer; NOT an FK
                                                 -- so receipts/attempts survive
                                                 -- calendar-row deletion
  source_url            text not null,
  source_md5            text not null check (source_md5 ~ '^md5:[0-9a-f]{32}$'),
  delivered_url         text not null,
  delivered_md5         text not null check (delivered_md5 ~ '^md5:[0-9a-f]{32}$'),
  delivered_phash       char(16) not null check (delivered_phash ~ '^[0-9a-f]{16}$'),
  source_read_receipt   uuid not null,
  delivered_read_receipt uuid not null,
  render_receipt        uuid,                    -- required iff bytes differ
  -- Round-3 P2: expected provider/channel committed at prepare and frozen
  -- by the attempt guard. expected_channel is the row's account (prepare
  -- refuses a row with none); expected_provider is the caller-declared
  -- send provider. The terminal attestation must match BOTH exactly, so
  -- an attestation cannot be transplanted across providers or channels.
  expected_provider     text not null check (btrim(expected_provider) <> ''),
  -- Round-4 P2: the caller-declared destination provider account id
  -- (from a verified provider connection), bound immutably at prepare;
  -- the terminal attestation must match it EXACTLY.
  expected_provider_account_id text not null
    check (btrim(expected_provider_account_id) <> ''),
  expected_channel      text not null check (btrim(expected_channel) <> ''),
  state                 text not null default 'prepared' check (state in
    ('prepared','confirmed_delivered','confirmed_no_send')),
  provider_post_id      text,
  outcome_evidence      jsonb,
  outcome_recorded_at   timestamptz,
  receipt_id            uuid,                    -- set only for the attempt
                                                 -- that minted the receipt
  superseded_by_receipt uuid,                    -- delivered, but a sibling
                                                 -- delivered first
  prepared_at           timestamptz not null default now(),
  check ((source_url = delivered_url and source_md5 = delivered_md5)
         = (render_receipt is null)),
  check ((state = 'confirmed_delivered')
         = (provider_post_id is not null)),
  check ((state = 'prepared') = (outcome_recorded_at is null)),
  foreign key (ledger_gym_id, group_key)
    references public.visual_group_usage_ledger (gym_id, group_key),
  foreign key (source_read_receipt)
    references public.visual_global_object_read_receipt (receipt_id),
  foreign key (delivered_read_receipt)
    references public.visual_global_object_read_receipt (receipt_id),
  foreign key (render_receipt)
    references public.visual_global_render_receipt (receipt_id)
);

comment on table public.visual_scene_original_use_attempt is
  'DRAFT/UNAPPLIED/OFF: pre-send PREPARED provider attempts for visual scene '
  'original-use proof. One per exact claim token. A prepared row is evidence '
  'that pre-send byte/read/render/reservation evidence existed; it is NEVER '
  'proof the provider was called or that anything was delivered. Terminal '
  'outcomes come only from visual_scene_original_use_terminate() and require '
  'a matching owner-only attestation. Unknown provider outcomes stay '
  'prepared forever (held); the send is not replayed.';

-- ----------------------------------------------------------------------------
-- Authoritative provider outcome attestation (owner-only, append-only).
-- The SEPARATE attester integration (NOT built yet — terminal outcomes are
-- impossible until it exists) writes exactly one row per claim token after
-- an authoritative provider readback ('delivered') or an authoritative
-- absence check ('confirmed_no_send'). service_role can read but can never
-- write this table, so the public terminal RPC cannot be used to forge a
-- delivery. This package records attestation; it does not create it.
-- ----------------------------------------------------------------------------
create table if not exists public.visual_scene_original_use_attestation (
  attestation_id      uuid primary key default gen_random_uuid(),
  -- Exact claim-token binding: one attestation per prepared attempt token.
  claim_attempt_id    uuid not null unique
    references public.visual_scene_original_use_attempt (claim_attempt_id),
  tenant_id           text not null,
  calendar_row_id     uuid not null,           -- evidence pointer, no FK
  outcome             text not null check (outcome in
    ('delivered','confirmed_no_send')),
  provider            text not null check (btrim(provider) <> ''),
  provider_account_id text not null check (btrim(provider_account_id) <> ''),
  channel             text not null check (btrim(channel) <> ''),
  provider_post_id    text check (provider_post_id is null
    or btrim(provider_post_id) <> ''),
  -- Delivered byte lineage: terminate requires these to equal the prepared
  -- attempt's delivered object exactly, so the attestation cannot be
  -- transplanted onto different bytes.
  delivered_url       text not null,
  delivered_md5       text not null check (delivered_md5 ~ '^md5:[0-9a-f]{32}$'),
  delivered_phash     char(16) not null check (delivered_phash ~ '^[0-9a-f]{16}$'),
  -- Authoritative provider readback / absence evidence. The attester owns
  -- its shape (free-form object) but it is NEVER empty: an attestation
  -- without evidence is not an attestation.
  readback_evidence   jsonb not null check (jsonb_typeof(readback_evidence)
    = 'object' and readback_evidence <> '{}'::jsonb),
  attested_by         text not null check (btrim(attested_by) <> ''),
  attested_at         timestamptz not null default now(),
  check ((outcome = 'delivered') = (provider_post_id is not null))
);

comment on table public.visual_scene_original_use_attestation is
  'DRAFT/UNAPPLIED/OFF: owner-only authoritative provider outcome '
  'attestation, one per prepared attempt claim token. Written ONLY by the '
  'separately reviewed attester integration (not built yet); service_role '
  'has SELECT only. Terminal delivery/no-send is impossible without a '
  'matching row. Append-only.';

alter table public.visual_scene_original_use_attestation
  enable row level security;
drop policy if exists visual_scene_original_use_attestation_service_read
  on public.visual_scene_original_use_attestation;
create policy visual_scene_original_use_attestation_service_read
  on public.visual_scene_original_use_attestation
  for select to service_role using (true);

-- ----------------------------------------------------------------------------
-- Phase-2 table: immutable original-use receipts, ONE per ledger event.
-- ----------------------------------------------------------------------------
create table if not exists public.visual_scene_original_use_receipt (
  receipt_id        uuid primary key default gen_random_uuid(),
  ledger_gym_id     text not null,             -- CANONICAL ledger PK part
  group_key         text not null,
  used_date         date not null,
  attempt_id        uuid not null unique
    references public.visual_scene_original_use_attempt (attempt_id),
  claim_attempt_id  uuid not null,
  attestation_id    uuid not null
    references public.visual_scene_original_use_attestation (attestation_id),
  tenant_id         text not null,
  calendar_row_id   uuid not null,   -- delivering row; evidence pointer, no FK
  delivered_url     text not null,
  delivered_md5     text not null,
  delivered_phash   char(16) not null,
  provider_post_id  text not null check (btrim(provider_post_id) <> ''),
  minted_at         timestamptz not null default now(),
  -- CAS key: the first confirmed delivery of this ledger event wins. Every
  -- later confirmed same-day sibling is recorded on its own attempt with
  -- superseded_by_receipt set and NEVER mints a second receipt.
  unique (ledger_gym_id, group_key, used_date),
  foreign key (ledger_gym_id, group_key)
    references public.visual_group_usage_ledger (gym_id, group_key)
);

comment on table public.visual_scene_original_use_receipt is
  'DRAFT/UNAPPLIED/OFF: immutable original-use receipt — the FIRST '
  'authoritatively confirmed provider delivery for one ledger event '
  '(ledger_gym_id, group_key, used_date). Minted only inside '
  'visual_scene_original_use_terminate() from a prepared attempt with a '
  'matching owner-only attestation; never by a trigger, never from '
  'published status alone. Append-only.';

-- ----------------------------------------------------------------------------
-- Append-only guards. PostgreSQL supports TRUNCATE ONLY on FOR EACH
-- STATEMENT triggers (round-2 P0): each table gets a FOR EACH ROW
-- UPDATE/DELETE trigger PLUS a separate FOR EACH STATEMENT TRUNCATE trigger.
-- ----------------------------------------------------------------------------
create or replace function public.visual_scene_original_use_receipt_immutable()
returns trigger language plpgsql as $$
begin
  raise exception 'visual_scene_original_use_receipt is append-only'
    using errcode = '23514';
end $$;

drop trigger if exists visual_scene_original_use_receipt_immutable
  on public.visual_scene_original_use_receipt;
drop trigger if exists visual_scene_original_use_receipt_immutable_truncate
  on public.visual_scene_original_use_receipt;
create trigger visual_scene_original_use_receipt_immutable
  before update or delete
  on public.visual_scene_original_use_receipt
  for each row execute function
    public.visual_scene_original_use_receipt_immutable();
create trigger visual_scene_original_use_receipt_immutable_truncate
  before truncate
  on public.visual_scene_original_use_receipt
  for each statement execute function
    public.visual_scene_original_use_receipt_immutable();

create or replace function public.visual_scene_original_use_attestation_immutable()
returns trigger language plpgsql as $$
begin
  raise exception 'visual_scene_original_use_attestation is append-only'
    using errcode = '23514';
end $$;

drop trigger if exists visual_scene_original_use_attestation_immutable
  on public.visual_scene_original_use_attestation;
drop trigger if exists visual_scene_original_use_attestation_immutable_truncate
  on public.visual_scene_original_use_attestation;
create trigger visual_scene_original_use_attestation_immutable
  before update or delete
  on public.visual_scene_original_use_attestation
  for each row execute function
    public.visual_scene_original_use_attestation_immutable();
create trigger visual_scene_original_use_attestation_immutable_truncate
  before truncate
  on public.visual_scene_original_use_attestation
  for each statement execute function
    public.visual_scene_original_use_attestation_immutable();

create or replace function public.visual_scene_original_use_attempt_guard()
returns trigger language plpgsql as $$
begin
  if tg_op = 'DELETE' or tg_op = 'TRUNCATE' then
    raise exception 'visual_scene_original_use_attempt is append-only'
      using errcode = '23514';
  end if;
  -- UPDATE: pre-send evidence columns are frozen at prepare time; only the
  -- terminal outcome fields may change, exactly once.
  if old.state <> 'prepared' then
    raise exception 'visual_scene_original_use_attempt outcome is final'
      using errcode = '23514';
  end if;
  if new.claim_attempt_id is distinct from old.claim_attempt_id
     or new.tenant_id is distinct from old.tenant_id
     or new.ledger_gym_id is distinct from old.ledger_gym_id
     or new.group_key is distinct from old.group_key
     or new.used_date is distinct from old.used_date
     or new.calendar_row_id is distinct from old.calendar_row_id
     or new.source_url is distinct from old.source_url
     or new.source_md5 is distinct from old.source_md5
     or new.delivered_url is distinct from old.delivered_url
     or new.delivered_md5 is distinct from old.delivered_md5
     or new.delivered_phash is distinct from old.delivered_phash
     or new.source_read_receipt is distinct from old.source_read_receipt
     or new.delivered_read_receipt is distinct from old.delivered_read_receipt
     or new.render_receipt is distinct from old.render_receipt
     or new.expected_provider is distinct from old.expected_provider
     or new.expected_provider_account_id
        is distinct from old.expected_provider_account_id
     or new.expected_channel is distinct from old.expected_channel then
    raise exception 'prepared attempt evidence is immutable'
      using errcode = '23514';
  end if;
  return new;
end $$;

drop trigger if exists visual_scene_original_use_attempt_guard
  on public.visual_scene_original_use_attempt;
drop trigger if exists visual_scene_original_use_attempt_guard_truncate
  on public.visual_scene_original_use_attempt;
create trigger visual_scene_original_use_attempt_guard
  before update or delete
  on public.visual_scene_original_use_attempt
  for each row execute function
    public.visual_scene_original_use_attempt_guard();
create trigger visual_scene_original_use_attempt_guard_truncate
  before truncate
  on public.visual_scene_original_use_attempt
  for each statement execute function
    public.visual_scene_original_use_attempt_guard();

-- ----------------------------------------------------------------------------
-- Round-4 P1 fail-closed ambiguity gate (shared by prepare and terminate).
-- Refuses ANY unresolved historical sibling/hold context for this exact
-- calendar row that cannot be PROVED current: an unreconciled ambiguous
-- sibling needs the exact live claim token, the exact current delivered
-- image, and no original provider post other than the current one (p_post
-- is NULL pre-send, so ANY set original post refuses at prepare); an
-- uncovered review_hold needs per-claim evidence (JSON reason) whose
-- publish_claim_token and image_url match exactly and whose late_post_id /
-- post_date, when present, match. Anything else raises; the row stays HELD
-- for manual evidence recovery. Already-reconciled siblings / covered
-- holds are terminal history and are not re-judged here.
-- ----------------------------------------------------------------------------
create or replace function public.visual_scene_original_use_check_ambiguity(
  p_tenant text, p_row_id uuid, p_row_date date, p_token uuid,
  p_image text, p_post text)
returns void language plpgsql stable security definer
set search_path = public as $$
begin
  if exists (select 1 from public.visual_group_usage_sibling s
      where s.gym_id = p_tenant and s.calendar_row_id = p_row_id
        and s.ambiguous
        and not public.visual_group_sibling_reconciled(s)
        and (s.original_claim_token is null
             or s.original_claim_token is distinct from p_token
             or s.original_image_url is null
             or s.original_image_url is distinct from p_image
             or (s.original_provider_post_id is not null
                 and s.original_provider_post_id is distinct from p_post))) then
    raise exception 'unresolved historical sibling ambiguity: an ambiguous '
      'sibling of this row has a NULL/older/different claim token or an '
      'original image/provider post that cannot be proved current; the row '
      'stays held for manual evidence recovery'
      using errcode = '23514';
  end if;
  if exists (select 1 from public.visual_group_member_event e
      where e.gym_id = p_tenant and e.alias_value = p_row_id::text
        and e.action = 'review_hold'
        and e.actor in ('backfill_ambiguous_review',
                        'runtime_ambiguous_review')
        and not exists (select 1 from public.visual_group_reconciliation c
          where c.gym_id = e.gym_id and e.id = any(c.hold_event_ids))
        and (left(btrim(coalesce(e.reason, '')), 1) <> '{'
             or nullif(e.reason::jsonb ->> 'publish_claim_token', '') is null
             or (e.reason::jsonb ->> 'publish_claim_token')::uuid
                is distinct from p_token
             or nullif(e.reason::jsonb ->> 'image_url', '') is null
             or e.reason::jsonb ->> 'image_url' is distinct from p_image
             or (nullif(e.reason::jsonb ->> 'late_post_id', '') is not null
                 and e.reason::jsonb ->> 'late_post_id'
                     is distinct from p_post)
             or (nullif(e.reason::jsonb ->> 'post_date', '') is not null
                 and (e.reason::jsonb ->> 'post_date')::date
                     is distinct from p_row_date))) then
    raise exception 'unresolved review_hold without per-claim evidence '
      'proving it current: the hold cannot be cleared by inclusion and the '
      'row stays held for manual evidence recovery'
      using errcode = '23514';
  end if;
end $$;

-- ----------------------------------------------------------------------------
-- PHASE 1: pre-send prepare. Raises (no attempt, no provider call) unless
-- every evidence class is present and exactly bound.
-- ----------------------------------------------------------------------------
create or replace function public.visual_scene_original_use_prepare(p jsonb)
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  v_row       public.content_calendar%rowtype;
  v_ledger    public.visual_group_usage_ledger%rowtype;
  v_occ       public.visual_scene_phash_occupied%rowtype;
  v_tenant    text;
  v_holder    boolean;
  v_existing  public.visual_scene_original_use_attempt%rowtype;
  v_attempt   public.visual_scene_original_use_attempt%rowtype;
  v_token     uuid := nullif(p->>'claim_attempt_id','')::uuid;
  v_row_id    uuid := nullif(p->>'calendar_row_id','')::uuid;
  v_group     text := p->>'group_key';
  v_date      date := nullif(p->>'used_date','')::date;
  v_s_url     text := p->>'source_url';
  v_s_md5     text := p->>'source_md5';
  v_d_url     text := p->>'delivered_url';
  v_d_md5     text := p->>'delivered_md5';
  v_d_phash   text := p->>'delivered_phash';
  v_s_rr      uuid := nullif(p->>'source_read_receipt','')::uuid;
  v_d_rr      uuid := nullif(p->>'delivered_read_receipt','')::uuid;
  v_r_rr      uuid := nullif(p->>'render_receipt','')::uuid;
  v_provider  text := nullif(btrim(p->>'provider'),'');
  v_acct      text := nullif(btrim(coalesce(p->>'provider_account_id','')),'');
  v_channel   text;
begin
  if v_token is null or v_row_id is null or v_group is null or v_date is null
     or v_d_url is null or v_d_md5 is null or v_d_phash is null
     or v_s_url is null or v_s_md5 is null
     or v_s_rr is null or v_d_rr is null or v_provider is null
     or v_acct is null then
    raise exception 'prepare: missing required evidence fields'
      using errcode = '22023';
  end if;

  -- Idempotent retry of the SAME prepare returns the existing attempt; any
  -- field drift under the same token is a conflict, never a silent rewrite.
  select * into v_existing from public.visual_scene_original_use_attempt
    where claim_attempt_id = v_token;
  if found then
    if v_existing.calendar_row_id = v_row_id
       and v_existing.group_key = v_group
       and v_existing.used_date = v_date
       and v_existing.source_url = v_s_url
       and v_existing.source_md5 = v_s_md5
       and v_existing.delivered_url = v_d_url
       and v_existing.delivered_md5 = v_d_md5
       and v_existing.delivered_phash = v_d_phash
       and v_existing.expected_provider = v_provider
       and v_existing.expected_provider_account_id = v_acct
       and v_existing.source_read_receipt = v_s_rr
       and v_existing.delivered_read_receipt = v_d_rr
       and v_existing.render_receipt is not distinct from v_r_rr then
      return jsonb_build_object('attempt_id', v_existing.attempt_id,
        'state', v_existing.state, 'replayed', true);
    end if;
    raise exception 'claim token already prepared with different evidence'
      using errcode = '23505';
  end if;

  -- Lock the exact calendar row; bind the EXACT live claim token. A row
  -- already marked published is NOT provider-use proof and cannot prepare.
  select * into v_row from public.content_calendar
    where id = v_row_id for update;
  if not found then
    raise exception 'prepare: calendar row not found' using errcode = '22023';
  end if;
  if v_row.publish_claim_token is null
     or v_row.publish_claim_token <> v_token then
    raise exception 'prepare: claim token does not match the live row token'
      using errcode = '23505';
  end if;
  if v_row.status = 'published' or v_row.published_at is not null then
    raise exception 'prepare: row already marked published; published status '
      'is not provider-use proof' using errcode = '23514';
  end if;
  -- Canonical tenant, resolved EXACTLY like the claim flow (the scene claim
  -- scan uses visual_group_tenant_strict; the exact-byte guard canonicalizes
  -- the raw calendar key at the trigger boundary). A raw alias key resolves
  -- here; an unmapped key raises. EVERY internal key below is canonical —
  -- the ledger, sibling and occupancy tables are keyed by the canonical
  -- tenant, never by the row's raw gym alias.
  v_tenant := public.visual_group_tenant_strict(v_row.gym_id)::text;
  -- Round-3 P2: bind the row's channel account immutably. A row with no
  -- channel account cannot be bound to a provider delivery and refuses
  -- here, pre-send; the attestation must later match this exact value.
  v_channel := nullif(btrim(coalesce(v_row.account, '')), '');
  if v_channel is null then
    raise exception 'prepare: calendar row has no channel account; the '
      'expected provider/account/channel cannot be bound'
      using errcode = '23514';
  end if;
  if v_row.post_date is distinct from v_date
     or v_row.visual_group_key is distinct from v_group then
    raise exception 'prepare: claimed date/group do not match the row'
      using errcode = '23514';
  end if;

  -- CURRENT reservation OR same-day terminal publication (round-3 P1):
  -- the FIRST sibling's terminal publication moves this SHARED ledger row
  -- to 'published' (the armed claim trigger does this on finalization)
  -- while preserving reserved_date. A lawful LATER same-day channel
  -- sibling must still prepare, so a 'published' ledger on THIS exact
  -- reserved_date is a current reservation for this ledger event. A
  -- released ledger, any other date, a swapped row or a missing row still
  -- refuses here, before any provider call.
  select * into v_ledger from public.visual_group_usage_ledger
    where gym_id = v_tenant and group_key = v_group for update;
  if not found or v_ledger.state not in ('reserved','published')
     or v_ledger.reserved_date is distinct from v_date then
    raise exception 'prepare: no current ledger reservation for this '
      'group/date' using errcode = '23514';
  end if;
  v_holder := v_ledger.calendar_row_id is not distinct from v_row_id
    or exists (
      select 1 from public.visual_group_usage_sibling s
       where s.gym_id = v_tenant and s.group_key = v_group
         and s.calendar_row_id = v_row_id
         and s.state = 'active');
  if not v_holder then
    raise exception 'prepare: row is not the ledger reservation holder nor '
      'an active same-day sibling' using errcode = '23514';
  end if;

  -- Row binding (round-2 P1): the delivered URL must be the row's ACTUAL
  -- current delivered object — the claim stack's own
  -- visual_scene_row_delivered_object (poster = thumbnail_url for video
  -- rows, display = image_url otherwise). A caller-supplied URL that merely
  -- shares the md5 is rejected.
  if not exists (select 1 from public.visual_scene_row_delivered_object(v_row) d
      where d.exact_url = v_d_url) then
    raise exception 'prepare: delivered URL is not the row''s current '
      'delivered object (poster/display media field)'
      using errcode = '23514';
  end if;
  -- Source binding: when the row records a distinct source media object the
  -- claimed source URL must equal it; when it does not, there is no row
  -- evidence of a distinct source, so source must equal delivered (a
  -- caller-invented rendered source is unverifiable and refused).
  if nullif(btrim(coalesce(v_row.source_media_url, '')), '') is not null then
    if v_s_url is distinct from btrim(v_row.source_media_url) then
      raise exception 'prepare: source URL does not match the row''s '
        'source_media_url' using errcode = '23514';
    end if;
  elsif v_s_url is distinct from v_d_url
     or v_s_md5 is distinct from v_d_md5 then
    raise exception 'prepare: the row records no source_media_url, so a '
      'distinct rendered source cannot be verified; source must equal '
      'delivered' using errcode = '23514';
  end if;

  -- Byte lineage: owner read receipts must exist for BOTH objects, and when
  -- delivered bytes differ a render receipt must chain exactly those two
  -- reads. Free-form attestation is not accepted.
  if not exists (select 1 from public.visual_global_object_read_receipt r
      where r.receipt_id = v_s_rr and r.tenant_id = v_tenant
        and r.exact_url = v_s_url and r.fingerprint = v_s_md5) then
    raise exception 'prepare: source read receipt missing or mismatched'
      using errcode = '23514';
  end if;
  if not exists (select 1 from public.visual_global_object_read_receipt r
      where r.receipt_id = v_d_rr and r.tenant_id = v_tenant
        and r.exact_url = v_d_url and r.fingerprint = v_d_md5) then
    raise exception 'prepare: delivered read receipt missing or mismatched'
      using errcode = '23514';
  end if;
  if (v_s_url is distinct from v_d_url or v_s_md5 is distinct from v_d_md5)
     and not exists (select 1 from public.visual_global_render_receipt rr
       where rr.receipt_id = v_r_rr and rr.tenant_id = v_tenant
         and rr.source_read_receipt = v_s_rr
         and rr.delivered_read_receipt = v_d_rr
         and rr.source_exact_url = v_s_url
         and rr.delivered_exact_url = v_d_url
         and rr.source_fingerprint = v_s_md5
         and rr.delivered_fingerprint = v_d_md5) then
    raise exception 'prepare: render receipt missing or does not chain the '
      'claimed read receipts' using errcode = '23514';
  end if;

  -- CURRENT scene reservation evidence (round-2 P1): the SHARED occupancy
  -- row for this tenant/group/date — PK (phash, tenant_id, group_key,
  -- used_date) — must match the exact delivered bytes (fingerprint) AND the
  -- claim-recorded exact URL (evidence->>'exact_url', written by the claim
  -- path from the bound candidate). Same-date same-image siblings share ONE
  -- occupancy row whose calendar_row_id names whichever sibling recorded it
  -- first; membership was already proven above (ledger holder OR active
  -- sibling), so occupancy is deliberately NOT re-bound to this row's id.
  -- This is reservation evidence only; it is never provider-use proof.
  select * into v_occ from public.visual_scene_phash_occupied o
    where o.phash = v_d_phash and o.tenant_id = v_tenant
      and o.group_key = v_group and o.used_date = v_date;
  if not found or v_occ.fingerprint is distinct from v_d_md5
     or v_occ.evidence->>'exact_url' is distinct from v_d_url then
    raise exception 'prepare: no current scene occupancy reservation for '
      'this group/date and these exact delivered bytes'
      using errcode = '23514';
  end if;

  -- Round-4 P1 (PRE-SEND, fail closed): refuse unresolved historical
  -- sibling/hold context BEFORE the attempt row exists — no attempt means
  -- nothing a sender could authorize from. p_post is NULL here: ANY
  -- original provider post id on an unreconciled sibling/hold refuses.
  perform public.visual_scene_original_use_check_ambiguity(
    v_tenant, v_row_id, v_row.post_date, v_token, v_d_url, null);

  insert into public.visual_scene_original_use_attempt (
    claim_attempt_id, tenant_id, ledger_gym_id, group_key, used_date,
    calendar_row_id, source_url, source_md5, delivered_url, delivered_md5,
    delivered_phash, source_read_receipt, delivered_read_receipt,
    render_receipt, expected_provider, expected_provider_account_id,
    expected_channel)
  values (
    v_token, v_tenant, v_tenant, v_group, v_date,
    v_row_id, v_s_url, v_s_md5, v_d_url, v_d_md5,
    v_d_phash, v_s_rr, v_d_rr, v_r_rr, v_provider, v_acct, v_channel)
  returning * into v_attempt;

  return jsonb_build_object('attempt_id', v_attempt.attempt_id,
    'state', v_attempt.state, 'replayed', false);
end $$;

-- ----------------------------------------------------------------------------
-- PHASE 2: idempotent terminal outcome. Requires a matching owner-only
-- AUTHORITATIVE provider outcome attestation (round-2 P1); records the
-- outcome and mints the receipt only for the FIRST confirmed delivery of
-- the ledger event. Unknown outcomes are never accepted: callers must hold.
-- ----------------------------------------------------------------------------
create or replace function public.visual_scene_original_use_terminate(p jsonb)
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  v_attempt  public.visual_scene_original_use_attempt%rowtype;
  v_row      public.content_calendar%rowtype;
  v_receipt  public.visual_scene_original_use_receipt%rowtype;
  v_att      public.visual_scene_original_use_attestation%rowtype;
  v_token    uuid := nullif(p->>'claim_attempt_id','')::uuid;
  v_outcome  text := p->>'outcome';
  v_post     text := nullif(btrim(coalesce(p->>'provider_post_id','')),'');
  v_ev       jsonb := coalesce(p->'outcome_evidence','{}'::jsonb);
  v_groups   text[];
  v_sib_attempts jsonb;
  v_holds    bigint[];
  v_pub_at   timestamptz;
  v_recon_evidence jsonb;
begin
  if v_token is null or v_outcome not in ('delivered','confirmed_no_send') then
    -- 'unknown' is deliberately NOT an outcome: unknown stays held/prepared.
    raise exception 'terminate: outcome must be delivered or '
      'confirmed_no_send; unknown outcomes stay held'
      using errcode = '22023';
  end if;
  if v_outcome = 'delivered' and v_post is null then
    raise exception 'terminate: delivered requires provider_post_id'
      using errcode = '22023';
  end if;
  if jsonb_typeof(v_ev) <> 'object' then
    raise exception 'terminate: outcome_evidence must be a jsonb object'
      using errcode = '22023';
  end if;

  select * into v_attempt from public.visual_scene_original_use_attempt
    where claim_attempt_id = v_token for update;
  if not found then
    raise exception 'terminate: no prepared attempt for this claim token; '
      'a receipt is never minted without pre-send evidence'
      using errcode = '22023';
  end if;

  -- Idempotent replay: identical terminal outcome returns the recorded
  -- result; a conflicting outcome on a finalized attempt is a hard conflict.
  if v_attempt.state = 'confirmed_no_send' then
    if v_outcome = 'confirmed_no_send' then
      return jsonb_build_object('attempt_id', v_attempt.attempt_id,
        'state', v_attempt.state, 'replayed', true);
    end if;
    raise exception 'terminate: attempt already finalized confirmed_no_send'
      using errcode = '23505';
  end if;
  if v_attempt.state = 'confirmed_delivered' then
    if v_outcome = 'delivered'
       and v_attempt.provider_post_id = v_post then
      return jsonb_build_object('attempt_id', v_attempt.attempt_id,
        'state', v_attempt.state, 'receipt_id', v_attempt.receipt_id,
        'superseded_by_receipt', v_attempt.superseded_by_receipt,
        'replayed', true);
    end if;
    raise exception 'terminate: attempt already finalized with a different '
      'delivered outcome' using errcode = '23505';
  end if;

  -- CAS: the row must still hold THIS exact token (no swap, no release).
  select * into v_row from public.content_calendar
    where id = v_attempt.calendar_row_id for update;
  if not found or v_row.publish_claim_token is distinct from v_token then
    raise exception 'terminate: claim token no longer held by the row'
      using errcode = '23505';
  end if;
  -- Round-3 P2: the row's channel account must not have drifted since the
  -- immutable prepare-time binding.
  if nullif(btrim(coalesce(v_row.account, '')), '')
     is distinct from v_attempt.expected_channel then
    raise exception 'terminate: row channel account changed since prepare'
      using errcode = '23514';
  end if;

  -- Round-4 P1 (TERMINAL recheck, AFTER the attempt and row locks):
  -- ambiguity inserted or changed between prepare and terminate cannot be
  -- laundered into the reconciliation receipt. For 'delivered' the current
  -- post is the attested/payload post (verified against the attestation
  -- below); for 'confirmed_no_send' NO current post exists, so any
  -- original provider post id refuses. A raise here rolls the whole
  -- terminal transaction back: NO receipt, NO reconciliation, NO
  -- publication, NO historical clearance.
  perform public.visual_scene_original_use_check_ambiguity(
    v_attempt.tenant_id, v_row.id, v_row.post_date, v_token,
    v_attempt.delivered_url,
    case when v_outcome = 'delivered' then v_post end);

  -- AUTHORITATIVE ATTESTATION GATE (round-2 P1): the caller's claimed
  -- outcome is never accepted on its own. There must be an owner-only
  -- attestation row for THIS exact claim token — written by the separate
  -- attester integration, never by service_role — and it must match the
  -- attempt, row, outcome, provider post id and delivered byte lineage
  -- exactly. Without it the terminal outcome is IMPOSSIBLE (fail closed):
  -- until the attester integration exists, no terminal delivery can happen.
  select * into v_att from public.visual_scene_original_use_attestation
    where claim_attempt_id = v_token;
  if not found then
    raise exception 'terminate: no authoritative provider outcome '
      'attestation for this claim token; terminal outcomes are impossible '
      'until the separately reviewed attester integration writes one '
      '(service_role cannot forge one)' using errcode = '23514';
  end if;
  if v_att.outcome is distinct from v_outcome
     or v_att.tenant_id is distinct from v_attempt.tenant_id
     or v_att.calendar_row_id is distinct from v_attempt.calendar_row_id
     or v_att.delivered_url is distinct from v_attempt.delivered_url
     or v_att.delivered_md5 is distinct from v_attempt.delivered_md5
     or v_att.delivered_phash is distinct from v_attempt.delivered_phash
     or (v_outcome = 'delivered'
         and v_att.provider_post_id is distinct from v_post)
     or v_att.provider is distinct from v_attempt.expected_provider
     or v_att.provider_account_id
        is distinct from v_attempt.expected_provider_account_id
     or v_att.channel is distinct from v_attempt.expected_channel then
    raise exception 'terminate: attestation does not match this attempt, '
      'row, outcome or provider delivery' using errcode = '23505';
  end if;
  v_ev := v_ev || jsonb_build_object(
    'attestation_id', v_att.attestation_id,
    'attested_by', v_att.attested_by,
    'provider', v_att.provider,
    'provider_account_id', v_att.provider_account_id,
    'channel', v_att.channel);

  -- CURRENT-TRANSACTION FINALIZATION PROOF (round-3 P1): the calendar
  -- transition below fires the claim stack's content_calendar trigger,
  -- which — for an ARMED tenant — refuses any ambiguous row's
  -- finalization without a visual_group_reconciliation receipt written in
  -- THIS transaction (visual_group_finalization_requires_evidence /
  -- visual_group_finalization_evidenced: same txid_current(), exact
  -- original claim snapshot, every ambiguous sibling attempt covered, all
  -- review holds covered, exact date/media/provider-post/published-at
  -- match). Write it HERE, after the owner-only attestation gate, from
  -- validated attempt/attestation/row data only — never from caller
  -- free-form fields. service_role has no INSERT on
  -- visual_group_reconciliation, so this row exists only because the
  -- attestation gate passed; a forged or missing attestation means no
  -- reconciliation row and the armed trigger keeps refusing the
  -- publication. The existing trigger is NOT weakened.
  -- Round-4 P1: build the receipt ONLY from validated CURRENT ambiguity.
  -- Every unreconciled ambiguous sibling and uncovered review_hold for
  -- this row passed visual_scene_original_use_check_ambiguity above
  -- (exact current token, exact current image, no foreign provider post);
  -- anything else already raised. Non-ambiguous siblings need no coverage
  -- and are not copied. No unresolved historical attempt or hold is
  -- cleared by inclusion.
  select coalesce(array_agg(distinct s.group_key order by s.group_key),
                  '{}'::text[])
    into v_groups
    from public.visual_group_usage_sibling s
    where s.gym_id = v_attempt.tenant_id and s.calendar_row_id = v_row.id
      and s.ambiguous;
  if not v_attempt.group_key = any(v_groups) then
    v_groups := array_append(v_groups, v_attempt.group_key);
  end if;
  select coalesce(jsonb_agg(jsonb_build_object(
      'group_key', s.group_key, 'attempt_id', s.attempt_id::text,
      'claim_token', s.original_claim_token::text,
      'provider_post_id', s.original_provider_post_id)), '[]'::jsonb)
    into v_sib_attempts
    from public.visual_group_usage_sibling s
    where s.gym_id = v_attempt.tenant_id and s.calendar_row_id = v_row.id
      and s.ambiguous;
  select coalesce(array_agg(e.id), '{}'::bigint[]) into v_holds
    from public.visual_group_member_event e
    where e.gym_id = v_attempt.tenant_id and e.alias_value = v_row.id::text
      and e.action = 'review_hold'
      and e.actor in ('backfill_ambiguous_review',
                      'runtime_ambiguous_review');
  -- One timestamp feeds BOTH the reconciliation evidence and the calendar
  -- update so the trigger's exact published_at equality always holds.
  v_pub_at := coalesce(v_row.published_at, now());
  v_recon_evidence := jsonb_build_object(
    'calendar_date', v_row.post_date::text,
    'delivered_url', v_row.image_url,
    'provider_post_id', v_post,
    'published_at', case when v_outcome = 'delivered'
                         then v_pub_at::text end,
    'delivery', case when v_outcome = 'delivered'
                     then 'delivered' else 'not_delivered' end,
    'provider', v_att.provider,
    'provider_account_id', v_att.provider_account_id,
    'channel', v_att.channel,
    'claim_attempt_id', v_attempt.claim_attempt_id,
    'attempt_id', v_attempt.attempt_id,
    'attestation_id', v_att.attestation_id);
  insert into public.visual_group_reconciliation(
    gym_id, calendar_row_id, outcome, delivered_group_key,
    reserved_groups, attempts, hold_event_ids, original_claim,
    evidence, actor)
  values (
    v_attempt.tenant_id, v_row.id,
    case when v_outcome = 'delivered'
         then 'confirmed_published' else 'confirmed_not_sent' end,
    case when v_outcome = 'delivered' then v_row.visual_group_key end,
    v_groups, v_sib_attempts, v_holds,
    jsonb_build_object('publish_claim_token', v_token::text,
      'late_post_id', v_row.late_post_id),
    v_recon_evidence, 'visual_scene_original_use_terminate');

  if v_outcome = 'confirmed_no_send' then
    -- Authoritative absence: release the claim so a NEW token may prepare.
    update public.visual_scene_original_use_attempt set
      state = 'confirmed_no_send',
      outcome_evidence = v_ev,
      outcome_recorded_at = now()
      where attempt_id = v_attempt.attempt_id;
    update public.content_calendar set
      publish_claim_token = null
      where id = v_row.id;
    return jsonb_build_object('attempt_id', v_attempt.attempt_id,
      'state', 'confirmed_no_send', 'replayed', false);
  end if;

  -- delivered: mint the receipt only if this ledger event has none yet.
  insert into public.visual_scene_original_use_receipt (
    ledger_gym_id, group_key, used_date, attempt_id, claim_attempt_id,
    attestation_id, tenant_id, calendar_row_id, delivered_url, delivered_md5,
    delivered_phash, provider_post_id)
  values (
    v_attempt.ledger_gym_id, v_attempt.group_key, v_attempt.used_date,
    v_attempt.attempt_id, v_attempt.claim_attempt_id,
    v_att.attestation_id, v_attempt.tenant_id,
    v_attempt.calendar_row_id, v_attempt.delivered_url,
    v_attempt.delivered_md5, v_attempt.delivered_phash, v_post)
  on conflict (ledger_gym_id, group_key, used_date) do nothing
  returning * into v_receipt;

  if not found then
    -- A same-day sibling delivered first: this delivery is real but is NOT
    -- the original use of the ledger event. Record it honestly.
    select * into v_receipt from public.visual_scene_original_use_receipt r
      where r.ledger_gym_id = v_attempt.ledger_gym_id
        and r.group_key = v_attempt.group_key
        and r.used_date = v_attempt.used_date;
    if v_receipt.attempt_id = v_attempt.attempt_id then
      update public.visual_scene_original_use_attempt set
        state = 'confirmed_delivered', provider_post_id = v_post,
        outcome_evidence = v_ev, outcome_recorded_at = now(),
        receipt_id = v_receipt.receipt_id
        where attempt_id = v_attempt.attempt_id
        returning * into v_attempt;
    else
      update public.visual_scene_original_use_attempt set
        state = 'confirmed_delivered', provider_post_id = v_post,
        outcome_evidence = v_ev, outcome_recorded_at = now(),
        superseded_by_receipt = v_receipt.receipt_id
        where attempt_id = v_attempt.attempt_id
        returning * into v_attempt;
    end if;
  else
    update public.visual_scene_original_use_attempt set
      state = 'confirmed_delivered', provider_post_id = v_post,
      outcome_evidence = v_ev, outcome_recorded_at = now(),
      receipt_id = v_receipt.receipt_id
      where attempt_id = v_attempt.attempt_id
      returning * into v_attempt;
  end if;

  -- Mark the row published ONLY as part of the terminal transaction that
  -- recorded authoritative provider evidence; clear the spent token.
  update public.content_calendar set
    status = 'published', published_at = v_pub_at,
    late_post_id = v_post, publish_claim_token = null
    where id = v_row.id;

  return jsonb_build_object('attempt_id', v_attempt.attempt_id,
    'state', 'confirmed_delivered', 'receipt_id', v_attempt.receipt_id,
    'superseded_by_receipt', v_attempt.superseded_by_receipt,
    'replayed', false);
end $$;

-- Owner-only evidence. The service role may read all three tables and may
-- call the two RPCs; it can never write any of the tables directly, so it
-- cannot forge an attestation, attempt or receipt.
revoke all on public.visual_scene_original_use_attempt
  from public, anon, authenticated, service_role;
revoke all on public.visual_scene_original_use_receipt
  from public, anon, authenticated, service_role;
revoke all on public.visual_scene_original_use_attestation
  from public, anon, authenticated, service_role;
grant select on public.visual_scene_original_use_attempt to service_role;
grant select on public.visual_scene_original_use_receipt to service_role;
grant select on public.visual_scene_original_use_attestation to service_role;
-- Internal SECURITY DEFINER helper: only the owner-executed RPCs may call it.
revoke all on function public.visual_scene_original_use_check_ambiguity(
  text, uuid, date, uuid, text, text)
  from public, anon, authenticated, service_role;
revoke all on function public.visual_scene_original_use_prepare(jsonb)
  from public, anon, authenticated;
revoke all on function public.visual_scene_original_use_terminate(jsonb)
  from public, anon, authenticated;
grant execute on function public.visual_scene_original_use_prepare(jsonb)
  to service_role;
grant execute on function public.visual_scene_original_use_terminate(jsonb)
  to service_role;

commit;
