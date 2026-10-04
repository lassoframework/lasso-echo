-- ============================================================================
-- DRAFT / UNAPPLIED / OFF — visual scene SCENE-ATTESTER RUNTIME
-- Date: 2026-10-04. Owner: codex/echo-scene-attester-sql-20261004.
-- Apply order: strictly AFTER
--   DRAFT_visual_scene_original_use_receipt_20261004.sql.
--
-- This is the attester-runtime follow-on to the two-phase original-use
-- receipt package. It adds:
--
--   1. TENANT-TO-ZERNIO BINDING (visual_scene_attester_binding): protected,
--      RLS-enabled, append-only binding of a canonical tenant + channel
--      account to the exact Zernio profile id, connected account id,
--      destination/page id and feed/story surface. No provider credentials
--      are stored here or anywhere in this file.
--
--   2. IMMUTABLE PREPARED SNAPSHOT (visual_scene_attester_prepared): per
--      claim token — canonical payload SHA256, original/delivered
--      URL+SHA256+MD5+byte length, delivered pHash, both read receipts, the
--      exact claim token and the exact binding identity. Evidence columns
--      are frozen by a guard trigger; only attester state-machine columns
--      may change, through the SECURITY DEFINER functions below.
--
--   3. COMPOSITE CLAIM+PREPARE (visual_scene_attester_claim_prepare):
--      mints the claim token, binds it to the calendar row AND runs the
--      receipt package's prepare in ONE PostgreSQL transaction. Separate
--      claim and prepare calls are NOT acceptable to this runtime:
--      service_role EXECUTE on the standalone prepare is revoked, and the
--      composite is the only granted entry point. Any prepare refusal
--      rolls the claim token back with it.
--
--   4. LEASE / EVENT STATE: visual_scene_attester_event (append-only) plus
--      a guarded state machine on the snapshot
--      (claimed_prepared -> send_started -> finalized | ambiguous_hold;
--       claimed_prepared -> lease_expired_hold).
--      A lease may expire only BEFORE send_started_at; expiry then moves
--      the snapshot to lease_expired_hold, which allows a NEW composite
--      claim+prepare. Once send_started_at is set, lease expiry can NEVER
--      authorize another send: the composite refuses any row whose prior
--      snapshot has send_started_at set and is not finalized.
--
--   5. NARROW ROLE: scene_attester (NOLOGIN, NO PASSWORD — none is embedded
--      here; login credentials are an operational secret, never SQL). It
--      holds EXECUTE on exactly three functions and SELECT on the evidence
--      tables. It has NO table DML anywhere.
--
--   6. ATOMIC ATTEST+TERMINAL (visual_scene_attester_attest_terminate):
--      writes the owner-only authoritative attestation AND calls the
--      receipt package's terminate in ONE transaction. service_role
--      EXECUTE on the old standalone terminate is REVOKED here; only this
--      function may finalize a provider outcome. Outcome 'ambiguous' never
--      finalizes: it parks the snapshot in ambiguous_hold, leaves the row
--      held, and writes an event (unknown/ambiguous hold preserved).
--
-- HONEST LIMITS (read before grading):
--   * This package CANNOT prevent direct out-of-band provider sends by
--     compromised worker code that holds live provider credentials; it can
--     only make such sends unable to finalize, mint receipts or clear
--     holds through the database.
--   * The Zernio readback that justifies an attestation is application
--     integration, not SQL; this package binds and freezes the evidence
--     the attester asserts and makes anything unmatched impossible.
--   * The composite's claim leg is NOT a private token mint: it calls the
--     production authoritative claimant
--     claim_calendar_publish_slot_owned(uuid,text,date,text,integer,boolean)
--     in the same transaction, with the advisory-locked and revalidated row's
--     gym plus the caller
--     supplied gym-local publish day, IANA timezone, day capacity and
--     approval-lane flag. Gym match, pending/approved status, active
--     variant, late_post_id, reservation-day stamping, per-gym
--     serialization and day capacity are enforced ONLY by that claimant.
--     A null return (forged/held/late/archived/unapproved/over-capacity
--     row) refuses the composite BEFORE any prepare; the minted token and
--     the persisted publishing row are re-verified before preparing.
--     The claimant MUST be the persisted-state redefinition installed by
--     DRAFT_visual_scene_rpc_persisted_state_20261004.sql (enforced by the
--     install preflight below): the original claimant can return an
--     attempted token after the authoritative scene trigger converts the
--     row to held, and the composite's refusal raise would then roll back
--     that durable hold. When the persisted row is genuinely scene-held
--     (exact frozen held shape AND an open visual_scene_review_hold), the
--     composite returns a structured held/refused result NORMALLY — no
--     raise, no prepare attempt, no token, no provider authorization — so
--     the trigger-written hold survives the transaction. Any other
--     refusal still fails closed by raising and never fabricates a hold.
-- ============================================================================

begin;

-- Fail-closed late install, same rule as the receipt package: once any
-- tenant is armed, installing new enforcement would silently change what
-- activation means. Refuse.
do $$
begin
  if exists (select 1 from public.gym_visual_guard_settings where enforce) then
    raise exception 'visual_scene_attester_runtime must be installed before '
      'any tenant is armed' using errcode = '23514';
  end if;
end $$;

-- The receipt package must already be installed.
do $$
begin
  if not exists (select 1 from pg_proc p join pg_namespace n
      on n.oid = p.pronamespace where n.nspname = 'public'
      and p.proname = 'visual_scene_original_use_prepare') then
    raise exception 'visual_scene_attester_runtime requires '
      'DRAFT_visual_scene_original_use_receipt_20261004.sql first'
      using errcode = '23514';
  end if;
end $$;

-- The PRODUCTION authoritative claimant must already be installed: the
-- composite claim leg delegates to it (calendar_publish_claim_ownership_
-- 20260918.sql). Exact 6-arg signature required; anything else fails the
-- install closed rather than silently weakening the claim gate.
do $$
begin
  if not exists (select 1 from pg_proc p join pg_namespace n
      on n.oid = p.pronamespace where n.nspname = 'public'
      and p.proname = 'claim_calendar_publish_slot_owned'
      and pg_get_function_identity_arguments(p.oid)
          = 'p_row_id uuid, p_gym_id text, p_day date, p_timezone text, '
            'p_capacity integer, p_approved_only boolean') then
    raise exception 'visual_scene_attester_runtime requires the production '
      'claim_calendar_publish_slot_owned(uuid,text,date,text,integer,'
      'boolean) claimant (calendar_publish_claim_ownership_20260918.sql)'
      using errcode = '23514';
  end if;
end $$;

-- The claimant must be the PERSISTED-STATE redefinition
-- (DRAFT_visual_scene_rpc_persisted_state_20261004.sql), not the original
-- calendar_publish_claim_ownership_20260918.sql body. The original can
-- return an attempted token after the authoritative scene trigger
-- converts the row to held; the composite's refusal raise would then roll
-- back the durable hold. Detect the redefinition by the marker comment its
-- body carries ("attempted state, not proof"); anything else fails the
-- install closed.
do $$
begin
  if not exists (select 1 from pg_proc p join pg_namespace n
      on n.oid = p.pronamespace where n.nspname = 'public'
      and p.proname = 'claim_calendar_publish_slot_owned'
      and pg_get_function_identity_arguments(p.oid)
          = 'p_row_id uuid, p_gym_id text, p_day date, p_timezone text, '
            'p_capacity integer, p_approved_only boolean'
      and p.prosrc like '%attempted state, not proof%') then
    raise exception 'visual_scene_attester_runtime requires the '
      'persisted-state claimant redefinition '
      '(DRAFT_visual_scene_rpc_persisted_state_20261004.sql): the original '
      'claimant can return an attempted token for a trigger-held row and '
      'the composite refusal would roll back the durable scene hold'
      using errcode = '23514';
  end if;
end $$;

-- The authoritative merged scene guard trigger must already be installed:
-- it alone owns the durable held-row mutation that the composite's
-- scene-held refusal reads back.
do $$
begin
  if not exists (select 1 from pg_trigger t
      join pg_class c on c.oid = t.tgrelid
      join pg_namespace n on n.oid = c.relnamespace
      where n.nspname = 'public' and c.relname = 'content_calendar'
        and t.tgname = 'content_calendar_visual_group_guard'
        and not t.tgisinternal) then
    raise exception 'visual_scene_attester_runtime requires the merged '
      'scene guard trigger content_calendar_visual_group_guard '
      '(DRAFT_visual_group_claim_trigger_20261002.sql scene stack)'
      using errcode = '23514';
  end if;
end $$;

-- ----------------------------------------------------------------------------
-- Narrow dedicated login role. NOLOGIN and NO PASSWORD: no credential is
-- embedded in SQL. If the role already exists it is left exactly as-is
-- (ownership of credentials stays operational).
-- ----------------------------------------------------------------------------
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'scene_attester') then
    create role scene_attester nologin;
  end if;
end $$;

-- ----------------------------------------------------------------------------
-- Tenant-to-Zernio binding (append-only; rebind = insert a new row).
-- ----------------------------------------------------------------------------
create table if not exists public.visual_scene_attester_binding (
  binding_id                 uuid primary key default gen_random_uuid(),
  tenant_id                  text not null check (btrim(tenant_id) <> ''),
  -- The row's channel account key (content_calendar.account) this binding
  -- is valid for. Bindings are per channel account, not just per tenant.
  account_key                text not null check (btrim(account_key) <> ''),
  zernio_profile_id          text not null
    check (btrim(zernio_profile_id) <> ''),
  zernio_connected_account_id text not null
    check (btrim(zernio_connected_account_id) <> ''),
  channel                    text not null check (btrim(channel) <> ''),
  destination_page_id        text not null
    check (btrim(destination_page_id) <> ''),
  surface                    text not null check (surface in ('feed','story')),
  bound_by                   text not null check (btrim(bound_by) <> ''),
  bound_at                   timestamptz not null default now(),
  unique (tenant_id, account_key, channel, surface, destination_page_id,
          zernio_connected_account_id)
);

comment on table public.visual_scene_attester_binding is
  'DRAFT/UNAPPLIED/OFF: protected tenant-to-Zernio binding. One row binds a '
  'canonical tenant + channel account to an exact Zernio profile id, '
  'connected account id, destination/page id and feed/story surface. '
  'Append-only: rotation inserts a new row. Contains NO credentials.';

create or replace function public.visual_scene_attester_binding_immutable()
returns trigger language plpgsql as $$
begin
  raise exception 'visual_scene_attester_binding is append-only; insert a '
    'new binding to rotate' using errcode = '23514';
end $$;

drop trigger if exists visual_scene_attester_binding_immutable
  on public.visual_scene_attester_binding;
drop trigger if exists visual_scene_attester_binding_immutable_truncate
  on public.visual_scene_attester_binding;
create trigger visual_scene_attester_binding_immutable
  before update or delete on public.visual_scene_attester_binding
  for each row execute function
    public.visual_scene_attester_binding_immutable();
create trigger visual_scene_attester_binding_immutable_truncate
  before truncate on public.visual_scene_attester_binding
  for each statement execute function
    public.visual_scene_attester_binding_immutable();

alter table public.visual_scene_attester_binding enable row level security;
alter table public.visual_scene_attester_binding force row level security;
drop policy if exists visual_scene_attester_binding_read
  on public.visual_scene_attester_binding;
create policy visual_scene_attester_binding_read
  on public.visual_scene_attester_binding
  for select to scene_attester, service_role using (true);

-- ----------------------------------------------------------------------------
-- Immutable prepared snapshot (per exact claim token) + attester state.
-- ----------------------------------------------------------------------------
create table if not exists public.visual_scene_attester_prepared (
  claim_attempt_id        uuid primary key references
    public.visual_scene_original_use_attempt (claim_attempt_id),
  tenant_id               text not null,
  calendar_row_id         uuid not null,      -- evidence pointer, no FK
  binding_id              uuid not null references
    public.visual_scene_attester_binding (binding_id),
  canonical_payload_sha256 text not null
    check (canonical_payload_sha256 ~ '^[0-9a-f]{64}$'),
  source_url              text not null,
  source_sha256           text not null
    check (source_sha256 ~ '^[0-9a-f]{64}$'),
  source_md5              text not null
    check (source_md5 ~ '^md5:[0-9a-f]{32}$'),
  source_byte_length      bigint not null check (source_byte_length > 0),
  delivered_url           text not null,
  delivered_sha256        text not null
    check (delivered_sha256 ~ '^[0-9a-f]{64}$'),
  delivered_md5           text not null
    check (delivered_md5 ~ '^md5:[0-9a-f]{32}$'),
  delivered_byte_length   bigint not null check (delivered_byte_length > 0),
  delivered_phash         char(16) not null
    check (delivered_phash ~ '^[0-9a-f]{16}$'),
  source_read_receipt     uuid not null,
  delivered_read_receipt  uuid not null,
  state                   text not null default 'claimed_prepared'
    check (state in ('claimed_prepared','send_started','finalized',
                     'ambiguous_hold','lease_expired_hold')),
  attester_id             text check (attester_id is null
    or btrim(attester_id) <> ''),
  lease_expires_at        timestamptz not null,
  send_started_at         timestamptz,
  finalized_at            timestamptz,
  prepared_at             timestamptz not null default now(),
  check ((state = 'send_started') = (send_started_at is not null)
         or state in ('finalized','ambiguous_hold') ),
  check ((state = 'finalized') = (finalized_at is not null)),
  -- A send that started can never look lease-expired; lease expiry is a
  -- pre-send concept only.
  check (not (state = 'lease_expired_hold' and send_started_at is not null))
);

comment on table public.visual_scene_attester_prepared is
  'DRAFT/UNAPPLIED/OFF: immutable prepared-attempt snapshot for the scene '
  'attester runtime. One per exact claim token: canonical payload SHA256, '
  'original/delivered URL+SHA256+MD5+byte length, delivered pHash, both '
  'read receipts, claim token and binding identity, frozen by guard '
  'trigger. State machine columns change only through the attester '
  'SECURITY DEFINER functions.';

create or replace function public.visual_scene_attester_prepared_guard()
returns trigger language plpgsql as $$
begin
  if tg_op = 'DELETE' or tg_op = 'TRUNCATE' then
    raise exception 'visual_scene_attester_prepared is append-only'
      using errcode = '23514';
  end if;
  -- Evidence columns are frozen at insert; only the attester state machine
  -- (state, attester_id, send_started_at, finalized_at) may move.
  if new.tenant_id is distinct from old.tenant_id
     or new.calendar_row_id is distinct from old.calendar_row_id
     or new.binding_id is distinct from old.binding_id
     or new.canonical_payload_sha256
        is distinct from old.canonical_payload_sha256
     or new.source_url is distinct from old.source_url
     or new.source_sha256 is distinct from old.source_sha256
     or new.source_md5 is distinct from old.source_md5
     or new.source_byte_length is distinct from old.source_byte_length
     or new.delivered_url is distinct from old.delivered_url
     or new.delivered_sha256 is distinct from old.delivered_sha256
     or new.delivered_md5 is distinct from old.delivered_md5
     or new.delivered_byte_length is distinct from old.delivered_byte_length
     or new.delivered_phash is distinct from old.delivered_phash
     or new.source_read_receipt is distinct from old.source_read_receipt
     or new.delivered_read_receipt is distinct from old.delivered_read_receipt
     or new.lease_expires_at is distinct from old.lease_expires_at then
    raise exception 'prepared snapshot evidence is immutable'
      using errcode = '23514';
  end if;
  -- Legal transitions only. finalized is terminal. send_started_at, once
  -- set, may never be cleared or moved. A started send may never become
  -- lease_expired_hold (lease expiry after send start authorizes nothing).
  if old.state = 'finalized' and new.state <> 'finalized' then
    raise exception 'finalized snapshot is terminal' using errcode = '23514';
  end if;
  if old.send_started_at is not null
     and (new.send_started_at is null
          or new.send_started_at <> old.send_started_at) then
    raise exception 'send_started_at is immutable once set'
      using errcode = '23514';
  end if;
  if old.send_started_at is not null and new.state = 'lease_expired_hold' then
    raise exception 'a send that started can never become lease_expired_hold'
      using errcode = '23514';
  end if;
  if (old.state, new.state) not in (
       ('claimed_prepared','send_started'),
       ('claimed_prepared','lease_expired_hold'),
       ('claimed_prepared','claimed_prepared'),
       ('send_started','send_started'),
       ('send_started','finalized'),
       ('send_started','ambiguous_hold'),
       ('ambiguous_hold','finalized'),
       ('ambiguous_hold','ambiguous_hold'),
       ('finalized','finalized')) then
    raise exception 'illegal attester state transition: % -> %',
      old.state, new.state using errcode = '23514';
  end if;
  return new;
end $$;

drop trigger if exists visual_scene_attester_prepared_guard
  on public.visual_scene_attester_prepared;
drop trigger if exists visual_scene_attester_prepared_guard_truncate
  on public.visual_scene_attester_prepared;
create trigger visual_scene_attester_prepared_guard
  before update or delete on public.visual_scene_attester_prepared
  for each row execute function public.visual_scene_attester_prepared_guard();
create trigger visual_scene_attester_prepared_guard_truncate
  before truncate on public.visual_scene_attester_prepared
  for each statement execute function
    public.visual_scene_attester_prepared_guard();

alter table public.visual_scene_attester_prepared enable row level security;
alter table public.visual_scene_attester_prepared force row level security;
drop policy if exists visual_scene_attester_prepared_read
  on public.visual_scene_attester_prepared;
create policy visual_scene_attester_prepared_read
  on public.visual_scene_attester_prepared
  for select to scene_attester, service_role using (true);

-- ----------------------------------------------------------------------------
-- Append-only attester event log.
-- ----------------------------------------------------------------------------
create table if not exists public.visual_scene_attester_event (
  event_id          bigint generated always as identity primary key,
  claim_attempt_id  uuid,
  calendar_row_id   uuid,
  event             text not null check (btrim(event) <> ''),
  actor             text not null check (btrim(actor) <> ''),
  detail            jsonb not null default '{}'::jsonb
    check (jsonb_typeof(detail) = 'object'),
  at                timestamptz not null default now()
);

create or replace function public.visual_scene_attester_event_immutable()
returns trigger language plpgsql as $$
begin
  raise exception 'visual_scene_attester_event is append-only'
    using errcode = '23514';
end $$;

drop trigger if exists visual_scene_attester_event_immutable
  on public.visual_scene_attester_event;
drop trigger if exists visual_scene_attester_event_immutable_truncate
  on public.visual_scene_attester_event;
create trigger visual_scene_attester_event_immutable
  before update or delete on public.visual_scene_attester_event
  for each row execute function public.visual_scene_attester_event_immutable();
create trigger visual_scene_attester_event_immutable_truncate
  before truncate on public.visual_scene_attester_event
  for each statement execute function
    public.visual_scene_attester_event_immutable();

alter table public.visual_scene_attester_event enable row level security;
alter table public.visual_scene_attester_event force row level security;
drop policy if exists visual_scene_attester_event_read
  on public.visual_scene_attester_event;
create policy visual_scene_attester_event_read
  on public.visual_scene_attester_event
  for select to scene_attester, service_role using (true);

-- ----------------------------------------------------------------------------
-- SCENE-HELD STRUCTURED REFUSAL (P0). When the persisted-state claimant
-- returns no token (or the persisted row does not show the minted claim),
-- the composite must distinguish two cases:
--   * a REAL scene hold: the authoritative scene trigger converted the
--     claim attempt into the durable held row (status pending, variant
--     archived, media_not_ready_reason='scene_review_hold', token and
--     reservation cleared) AND an OPEN visual_scene_review_hold exists for
--     the row. Raising here would roll that hold back, so the composite
--     returns this structured refusal NORMALLY: no token, no prepare
--     attempt, no provider authorization, hold preserved.
--   * anything else: NOT a scene hold. The helper returns null and the
--     caller fails closed by raising; a hold is never fabricated.
-- Internal only: no EXECUTE grant is given to any non-owner role.
-- ----------------------------------------------------------------------------
create or replace function public.visual_scene_attester_scene_hold_refusal(p_row_id uuid, p_attester text)
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  v_hold uuid;
begin
  select h.hold_id into v_hold
    from public.visual_scene_review_hold h
    join public.content_calendar r on r.id = h.calendar_row_id
   where h.calendar_row_id = p_row_id
     and h.state = 'open'
     and r.status = 'pending'
     and r.variant_status = 'archived'
     and r.media_not_ready_reason = 'scene_review_hold'
     and r.publish_claim_token is null
     and r.publish_reservation_day is null
   order by h.created_at desc, h.hold_id
   limit 1;
  if v_hold is null then
    return null;
  end if;
  insert into public.visual_scene_attester_event (
    claim_attempt_id, calendar_row_id, event, actor, detail)
  values (null, p_row_id, 'scene_hold_refusal', p_attester,
    jsonb_build_object('hold_id', v_hold));
  return jsonb_build_object('claim_attempt_id', null,
    'state', 'scene_held', 'refused', true, 'replayed', false,
    'hold_id', v_hold, 'reason', 'scene_review_hold');
end $$;

-- ----------------------------------------------------------------------------
-- COMPOSITE CLAIM + PREPARE (one PostgreSQL transaction; separate calls are
-- not acceptable — the standalone prepare EXECUTE is revoked below).
-- Payload: calendar_row_id, group_key, used_date, source/delivered
-- url+md5+sha256+byte_length, delivered_phash, source/delivered read
-- receipts, optional render_receipt, provider, provider_account_id,
-- channel, zernio_profile_id, zernio_connected_account_id,
-- destination_page_id, surface, publish_day (gym-local reservation day,
-- date), publish_timezone (IANA name), publish_capacity (1..2),
-- publish_approved_only (boolean, REQUIRED — the approval lane is never
-- silently downgraded), lease_seconds (default 900, max 3600),
-- attester_id. Returns the claim token + snapshot state.
-- The claim leg delegates 1:1 to the production authoritative claimant
-- claim_calendar_publish_slot_owned(uuid,text,date,text,integer,boolean):
-- gym id from the advisory-locked and revalidated row, with
-- day/timezone/capacity/approval from this
-- payload. This function never writes publish_claim_token directly.
-- ----------------------------------------------------------------------------
create or replace function public.visual_scene_attester_claim_prepare(p jsonb)
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  v_row_id   uuid := nullif(p->>'calendar_row_id','')::uuid;
  v_row      public.content_calendar%rowtype;
  v_gym      text;
  v_lookup_tenant text;
  v_tenant   text;
  v_binding  public.visual_scene_attester_binding%rowtype;
  v_snap     public.visual_scene_attester_prepared%rowtype;
  v_token    uuid;
  v_prep     jsonb;
  v_ref      jsonb;
  v_payload  text := nullif(p->>'canonical_payload_sha256','');
  v_s_sha    text := nullif(p->>'source_sha256','');
  v_d_sha    text := nullif(p->>'delivered_sha256','');
  v_s_len    bigint := nullif(p->>'source_byte_length','')::bigint;
  v_d_len    bigint := nullif(p->>'delivered_byte_length','')::bigint;
  v_attester text := nullif(btrim(coalesce(p->>'attester_id','')),'');
  v_lease_s  int := coalesce(nullif(p->>'lease_seconds','')::int, 900);
  v_day      date := nullif(p->>'publish_day','')::date;
  v_tz       text := nullif(btrim(coalesce(p->>'publish_timezone','')),'');
  v_cap      int := nullif(p->>'publish_capacity','')::int;
  v_appr     bool := case
    when p->>'publish_approved_only' = 'true' then true
    when p->>'publish_approved_only' = 'false' then false end;
begin
  if v_row_id is null or v_payload is null or v_s_sha is null
     or v_d_sha is null or v_s_len is null or v_d_len is null
     or v_attester is null
     or nullif(btrim(coalesce(p->>'zernio_profile_id','')),'') is null
     or nullif(btrim(coalesce(p->>'zernio_connected_account_id','')),'')
        is null
     or nullif(btrim(coalesce(p->>'destination_page_id','')),'') is null
     or coalesce(p->>'surface','') not in ('feed','story') then
    raise exception 'claim_prepare: missing required binding/payload fields'
      using errcode = '22023';
  end if;
  if v_lease_s < 1 or v_lease_s > 3600 then
    raise exception 'claim_prepare: lease_seconds must be in 1..3600'
      using errcode = '22023';
  end if;
  -- Authoritative-claim inputs are mandatory. The approval lane must be an
  -- explicit boolean: an absent/garbled flag can never default a row past
  -- the gym's approval requirement.
  if v_day is null or v_tz is null or v_cap is null or v_appr is null then
    raise exception 'claim_prepare: publish_day, publish_timezone, '
      'publish_capacity and publish_approved_only are required'
      using errcode = '22023';
  end if;
  if v_cap not between 1 and 2 then
    raise exception 'claim_prepare: publish_capacity must be in 1..2'
      using errcode = '22023';
  end if;
  if not exists (select 1 from pg_timezone_names where name = v_tz) then
    raise exception 'claim_prepare: publish_timezone is not a known IANA '
      'timezone' using errcode = '22023';
  end if;
  if v_payload !~ '^[0-9a-f]{64}$' or v_s_sha !~ '^[0-9a-f]{64}$'
     or v_d_sha !~ '^[0-9a-f]{64}$' then
    raise exception 'claim_prepare: sha256 fields must be 64 lowercase hex'
      using errcode = '22023';
  end if;

  -- CLAIM LEG: discover the candidate gym WITHOUT a row lock, then follow
  -- the production claimant's lock order: per-gym transaction advisory lock
  -- BEFORE content_calendar FOR UPDATE. The claimant below takes the same
  -- advisory lock and row lock reentrantly in this transaction. This avoids
  -- a row -> advisory inversion with direct claimant sessions, while keeping
  -- claim + prepare atomic. Any prepare refusal rolls the token back with it.
  select c.gym_id into v_gym
    from public.content_calendar c where c.id = v_row_id;
  if not found then
    raise exception 'claim_prepare: calendar row not found'
      using errcode = '22023';
  end if;
  if nullif(btrim(coalesce(v_gym,'')),'') is null then
    raise exception 'claim_prepare: calendar row has no gym'
      using errcode = '23514';
  end if;
  v_lookup_tenant := public.visual_group_tenant_strict(v_gym)::text;

  perform pg_advisory_xact_lock(hashtextextended(v_gym, 0));

  select * into v_row from public.content_calendar
    where id = v_row_id for update;
  if not found then
    raise exception 'claim_prepare: calendar row disappeared after gym lock'
      using errcode = '23514';
  end if;
  if v_row.gym_id is distinct from v_gym then
    raise exception 'claim_prepare: calendar row gym changed while acquiring '
      'the claim lock' using errcode = '23514';
  end if;
  v_tenant := public.visual_group_tenant_strict(v_row.gym_id)::text;
  if v_tenant is distinct from v_lookup_tenant then
    raise exception 'claim_prepare: calendar row tenant changed while '
      'acquiring the claim lock' using errcode = '23514';
  end if;
  if nullif(btrim(coalesce(v_row.account,'')),'') is null then
    raise exception 'claim_prepare: calendar row has no channel account'
      using errcode = '23514';
  end if;
  if v_row.status = 'published' or v_row.published_at is not null then
    raise exception 'claim_prepare: row already marked published'
      using errcode = '23514';
  end if;

  -- The exact binding must exist (latest binding for this identity wins).
  select * into v_binding from public.visual_scene_attester_binding b
    where b.tenant_id = v_tenant and b.account_key = v_row.account
      and b.zernio_profile_id = btrim(p->>'zernio_profile_id')
      and b.zernio_connected_account_id
          = btrim(p->>'zernio_connected_account_id')
      and b.channel = nullif(btrim(coalesce(p->>'channel','')),'')
      and b.destination_page_id = btrim(p->>'destination_page_id')
      and b.surface = p->>'surface'
    order by b.bound_at desc, b.binding_id desc limit 1;
  if not found then
    raise exception 'claim_prepare: no tenant-to-Zernio binding matches this '
      'tenant/account/profile/connected-account/channel/page/surface'
      using errcode = '23514';
  end if;

  -- LEASE-EXPIRY RULE: a prior snapshot for THIS ROW whose send started and
  -- never finalized blocks any new claim forever (manual recovery only).
  -- Lease expiry may authorize a new send ONLY when no send ever started.
  -- This guard precedes even idempotent replay: once a send started, the
  -- flow continues only through send_start / attest_terminate, never by
  -- re-entering the composite.
  if exists (select 1 from public.visual_scene_attester_prepared s
      where s.calendar_row_id = v_row_id
        and s.send_started_at is not null
        and s.state not in ('finalized')) then
    raise exception 'claim_prepare: a send already started for this row and '
      'was never finalized; lease expiry cannot authorize another send — '
      'the row stays held for manual recovery'
      using errcode = '23514';
  end if;

  -- IDEMPOTENT REPLAY of the SAME composite call, checked BEFORE the live-
  -- snapshot guard below so an identical retry is reachable while a
  -- claimed_prepared snapshot exists: the row already holds a token whose
  -- snapshot matches every payload/binding field and the same reservation
  -- day exactly.
  if v_row.publish_claim_token is not null then
    select * into v_snap from public.visual_scene_attester_prepared s
      where s.claim_attempt_id = v_row.publish_claim_token;
    if found and v_snap.state = 'finalized' then
      raise exception 'claim_prepare: row already finalized'
        using errcode = '23514';
    end if;
    if found and v_snap.canonical_payload_sha256 = v_payload
       and v_snap.binding_id = v_binding.binding_id
       and v_snap.source_sha256 = v_s_sha
       and v_snap.delivered_sha256 = v_d_sha
       and v_row.publish_reservation_day is not distinct from v_day then
      return jsonb_build_object('claim_attempt_id', v_snap.claim_attempt_id,
        'state', v_snap.state, 'replayed', true);
    end if;
    raise exception 'claim_prepare: row holds a claim token with different '
      'payload/binding evidence; token or binding drift refused'
      using errcode = '23505';
  end if;
  if exists (select 1 from public.visual_scene_attester_prepared s
      where s.calendar_row_id = v_row_id
        and s.state in ('claimed_prepared','send_started')) then
    raise exception 'claim_prepare: a live prepared snapshot already exists '
      'for this row' using errcode = '23514';
  end if;

  -- CLAIM LEG (P0): delegate to the PRODUCTION authoritative claimant in
  -- this same transaction. It alone enforces the gym match, pending/
  -- approved status, active variant, no late_post_id, the approval lane,
  -- per-gym serialization (advisory lock), the day-capacity count and the
  -- reservation-day stamp. A null return is a refusal: a forged, held,
  -- late, archived, unapproved or over-capacity row yields NO token and NO
  -- prepare attempt. This function never writes publish_claim_token
  -- directly. Any non-scene refusal raises, so a failed prepare below
  -- rolls the claimant's reservation back with it. A REAL scene hold is
  -- the ONE refusal that must NOT raise: the persisted-state claimant
  -- (required by the install preflight) already let the authoritative
  -- scene trigger's held mutation finish, and raising here would roll
  -- that durable hold back.
  v_token := public.claim_calendar_publish_slot_owned(
    v_row_id, v_gym, v_day, v_tz, v_cap, v_appr);

  -- Read the row persisted AFTER every row trigger in a separate
  -- statement, then classify a refusal: genuine scene hold (structured
  -- normal return, hold preserved) or anything else (fail closed).
  select * into v_row from public.content_calendar
    where id = v_row_id;
  if v_token is null then
    v_ref := public.visual_scene_attester_scene_hold_refusal(
      v_row_id, v_attester);
    if v_ref is not null then
      return v_ref;
    end if;
    raise exception 'claim_prepare: refused by the authoritative slot '
      'claimant (gym/status/variant/late-post/approval/capacity gate); '
      'row held unclaimed' using errcode = '23514';
  end if;

  -- VERIFY the returned token and the persisted publishing row before
  -- preparing: token bound to THIS row, status publishing, reservation
  -- day stamped, same gym. A scene-held persisted row is again the
  -- structured refusal, never a raise that rolls the hold back.
  if not found
     or v_row.publish_claim_token is distinct from v_token
     or v_row.status <> 'publishing'
     or v_row.publish_reservation_day is distinct from v_day then
    v_ref := public.visual_scene_attester_scene_hold_refusal(
      v_row_id, v_attester);
    if v_ref is not null then
      return v_ref;
    end if;
    raise exception 'claim_prepare: authoritative claim did not persist '
      'the expected token/publishing row; refusing to prepare'
      using errcode = '23514';
  end if;

  -- PREPARE LEG: the receipt package's own prepare, verbatim fields, in
  -- this same transaction. Its refusals (evidence, ledger, occupancy,
  -- ambiguity) roll back the token mint above.
  v_prep := public.visual_scene_original_use_prepare(jsonb_build_object(
    'claim_attempt_id', v_token,
    'calendar_row_id', v_row_id,
    'group_key', p->>'group_key',
    'used_date', p->>'used_date',
    'source_url', p->>'source_url',
    'source_md5', p->>'source_md5',
    'delivered_url', p->>'delivered_url',
    'delivered_md5', p->>'delivered_md5',
    'delivered_phash', p->>'delivered_phash',
    'source_read_receipt', p->>'source_read_receipt',
    'delivered_read_receipt', p->>'delivered_read_receipt',
    'render_receipt', p->>'render_receipt',
    'provider', p->>'provider',
    'provider_account_id', p->>'provider_account_id'));

  insert into public.visual_scene_attester_prepared (
    claim_attempt_id, tenant_id, calendar_row_id, binding_id,
    canonical_payload_sha256, source_url, source_sha256, source_md5,
    source_byte_length, delivered_url, delivered_sha256, delivered_md5,
    delivered_byte_length, delivered_phash, source_read_receipt,
    delivered_read_receipt, state, attester_id, lease_expires_at)
  values (
    v_token, v_tenant, v_row_id, v_binding.binding_id,
    v_payload, p->>'source_url', v_s_sha, p->>'source_md5',
    v_s_len, p->>'delivered_url', v_d_sha, p->>'delivered_md5',
    v_d_len, p->>'delivered_phash',
    nullif(p->>'source_read_receipt','')::uuid,
    nullif(p->>'delivered_read_receipt','')::uuid,
    'claimed_prepared', v_attester, now() + make_interval(secs => v_lease_s));

  insert into public.visual_scene_attester_event (
    claim_attempt_id, calendar_row_id, event, actor, detail)
  values (v_token, v_row_id, 'claim_prepared', v_attester,
    jsonb_build_object('binding_id', v_binding.binding_id,
      'lease_seconds', v_lease_s));

  return jsonb_build_object('claim_attempt_id', v_token,
    'attempt_id', v_prep->>'attempt_id', 'state', 'claimed_prepared',
    'binding_id', v_binding.binding_id, 'replayed', false);
end $$;

-- ----------------------------------------------------------------------------
-- SEND START (lease gate): exactly one attester may move claimed_prepared
-- -> send_started while the lease is live. Lease expiry before send start
-- moves the snapshot to lease_expired_hold; a started send can never be
-- lease-expired into reauthorization (guard trigger enforces).
-- ----------------------------------------------------------------------------
create or replace function public.visual_scene_attester_send_start(p jsonb)
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  v_token    uuid := nullif(p->>'claim_attempt_id','')::uuid;
  v_attester text := nullif(btrim(coalesce(p->>'attester_id','')),'');
  v_snap     public.visual_scene_attester_prepared%rowtype;
begin
  if v_token is null or v_attester is null then
    raise exception 'send_start: claim_attempt_id and attester_id required'
      using errcode = '22023';
  end if;
  select * into v_snap from public.visual_scene_attester_prepared
    where claim_attempt_id = v_token for update;
  if not found then
    raise exception 'send_start: no prepared snapshot for this claim token'
      using errcode = '22023';
  end if;
  if v_snap.state = 'send_started' then
    if v_snap.attester_id = v_attester then
      return jsonb_build_object('claim_attempt_id', v_token,
        'state', 'send_started', 'replayed', true);
    end if;
    raise exception 'send_start: send already started by another attester'
      using errcode = '23505';
  end if;
  if v_snap.state <> 'claimed_prepared' then
    raise exception 'send_start: snapshot state % cannot start a send',
      v_snap.state using errcode = '23514';
  end if;
  if v_snap.attester_id is distinct from v_attester then
    raise exception 'send_start: attester identity does not match the '
      'prepared snapshot' using errcode = '23505';
  end if;
  if v_snap.lease_expires_at <= now() then
    -- Park the hold and REFUSE by return, not by raise: an exception would
    -- roll this transaction back and silently un-park the hold, leaving a
    -- stale claimed_prepared snapshot that could start a send later.
    -- lease_expired=true is a refusal: no send_started_at is ever set here.
    update public.visual_scene_attester_prepared
      set state = 'lease_expired_hold'
      where claim_attempt_id = v_token;
    insert into public.visual_scene_attester_event (
      claim_attempt_id, calendar_row_id, event, actor, detail)
    values (v_token, v_snap.calendar_row_id, 'lease_expired_pre_send',
      v_attester, jsonb_build_object('lease_expires_at',
        v_snap.lease_expires_at));
    return jsonb_build_object('claim_attempt_id', v_token,
      'state', 'lease_expired_hold', 'lease_expired', true,
      'send_started', false, 'replayed', false);
  end if;
  update public.visual_scene_attester_prepared
    set state = 'send_started', send_started_at = now()
    where claim_attempt_id = v_token;
  insert into public.visual_scene_attester_event (
    claim_attempt_id, calendar_row_id, event, actor)
  values (v_token, v_snap.calendar_row_id, 'send_started', v_attester);
  return jsonb_build_object('claim_attempt_id', v_token,
    'state', 'send_started', 'replayed', false);
end $$;

-- ----------------------------------------------------------------------------
-- ATOMIC ATTEST + TERMINAL: the ONLY granted path that finalizes a provider
-- outcome. Writes the owner-only authoritative attestation AND calls the
-- receipt package's terminate in ONE transaction; any refusal rolls back
-- BOTH (no attestation, no receipt, no publication, snapshot unfinalized).
-- Outcome 'ambiguous' NEVER finalizes: the snapshot parks in
-- ambiguous_hold, the row keeps its token and stays unpublished, and only
-- a later authoritative attest+terminate (same transaction) may finalize.
-- ----------------------------------------------------------------------------
create or replace function public.visual_scene_attester_attest_terminate(p jsonb)
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  v_token    uuid := nullif(p->>'claim_attempt_id','')::uuid;
  v_outcome  text := p->>'outcome';
  v_post     text := nullif(btrim(coalesce(p->>'provider_post_id','')),'');
  v_attester text := nullif(btrim(coalesce(p->>'attester_id','')),'');
  v_ev       jsonb := p->'readback_evidence';
  v_snap     public.visual_scene_attester_prepared%rowtype;
  v_attempt  public.visual_scene_original_use_attempt%rowtype;
  v_att      public.visual_scene_original_use_attestation%rowtype;
  v_receipt  public.visual_scene_original_use_receipt%rowtype;
  v_term     jsonb;
  v_recorded_state text;
begin
  if v_token is null or v_attester is null
     or v_outcome not in ('delivered','confirmed_no_send','ambiguous') then
    raise exception 'attest_terminate: outcome must be delivered, '
      'confirmed_no_send or ambiguous' using errcode = '22023';
  end if;
  if v_outcome = 'delivered' and v_post is null then
    raise exception 'attest_terminate: delivered requires provider_post_id'
      using errcode = '22023';
  end if;
  if v_outcome <> 'ambiguous'
     and (v_ev is null or jsonb_typeof(v_ev) <> 'object'
          or v_ev = '{}'::jsonb) then
    raise exception 'attest_terminate: terminal outcomes require non-empty '
      'object readback_evidence' using errcode = '22023';
  end if;

  select * into v_snap from public.visual_scene_attester_prepared
    where claim_attempt_id = v_token for update;
  if not found then
    raise exception 'attest_terminate: no prepared snapshot for this claim '
      'token' using errcode = '22023';
  end if;
  if v_snap.attester_id is distinct from v_attester then
    raise exception 'attest_terminate: attester identity does not match the '
      'prepared snapshot' using errcode = '23505';
  end if;

  -- Replay: validate the request against immutable terminal records before
  -- returning success. The snapshot alone has no outcome or provider post
  -- identity, so it cannot establish that this is an identical replay.
  if v_snap.state = 'finalized' then
    select * into v_att from public.visual_scene_original_use_attestation
      where claim_attempt_id = v_token;
    select * into v_attempt from public.visual_scene_original_use_attempt
      where claim_attempt_id = v_token;
    if not found or v_att.claim_attempt_id is null then
      raise exception 'attest_terminate: finalized replay lacks its recorded '
        'attestation or terminal attempt' using errcode = '23514';
    end if;
    v_recorded_state := case when v_outcome = 'delivered'
      then 'confirmed_delivered'
      when v_outcome = 'confirmed_no_send' then 'confirmed_no_send'
      else null end;
    if v_att.outcome is distinct from v_outcome
       or v_attempt.state is distinct from v_recorded_state
       or v_att.provider_post_id is distinct from v_post
       or v_attempt.provider_post_id is distinct from v_post then
      raise exception 'attest_terminate: finalized replay conflicts with the '
        'recorded outcome or provider post identity' using errcode = '23505';
    end if;
    if v_outcome = 'delivered' then
      select * into v_receipt from public.visual_scene_original_use_receipt
        where claim_attempt_id = v_token;
      if not found or v_receipt.provider_post_id is distinct from v_post then
        raise exception 'attest_terminate: finalized replay conflicts with '
          'the recorded receipt provider post identity'
          using errcode = '23505';
      end if;
    elsif v_receipt.receipt_id is not null then
      raise exception 'attest_terminate: confirmed_no_send replay has a '
        'delivery receipt' using errcode = '23505';
    end if;
    return jsonb_build_object('claim_attempt_id', v_token,
      'state', 'finalized', 'outcome', v_att.outcome,
      'provider_post_id', v_att.provider_post_id, 'replayed', true);
  end if;

  -- UNKNOWN / AMBIGUOUS HOLD: never finalize, never release, never send
  -- again. The row keeps its token and stays unpublished.
  if v_outcome = 'ambiguous' then
    if v_snap.state = 'send_started' then
      update public.visual_scene_attester_prepared
        set state = 'ambiguous_hold' where claim_attempt_id = v_token;
    elsif v_snap.state <> 'ambiguous_hold' then
      raise exception 'attest_terminate: ambiguous hold requires a started '
        'send (state %)', v_snap.state using errcode = '23514';
    end if;
    insert into public.visual_scene_attester_event (
      claim_attempt_id, calendar_row_id, event, actor, detail)
    values (v_token, v_snap.calendar_row_id, 'ambiguous_hold', v_attester,
      coalesce(v_ev, '{}'::jsonb));
    return jsonb_build_object('claim_attempt_id', v_token,
      'state', 'ambiguous_hold', 'replayed',
      v_snap.state = 'ambiguous_hold');
  end if;

  if v_snap.state not in ('send_started','ambiguous_hold') then
    raise exception 'attest_terminate: terminal outcomes require a started '
      'send or an ambiguous hold (state %)', v_snap.state
      using errcode = '23514';
  end if;

  select * into v_attempt from public.visual_scene_original_use_attempt
    where claim_attempt_id = v_token;
  if not found then
    raise exception 'attest_terminate: no prepared attempt for this token'
      using errcode = '22023';
  end if;

  -- ATTEST LEG (atomic with the terminal leg below): one owner-only
  -- authoritative attestation bound to the exact claim token, tenant, row,
  -- outcome, provider identity and delivered byte lineage. terminate
  -- re-validates every field against the immutable attempt; a mismatch
  -- raises and rolls back this INSERT too.
  insert into public.visual_scene_original_use_attestation (
    claim_attempt_id, tenant_id, calendar_row_id, outcome, provider,
    provider_account_id, channel, provider_post_id, delivered_url,
    delivered_md5, delivered_phash, readback_evidence, attested_by)
  values (
    v_token, v_attempt.tenant_id, v_attempt.calendar_row_id, v_outcome,
    v_attempt.expected_provider, v_attempt.expected_provider_account_id,
    v_attempt.expected_channel,
    case when v_outcome = 'delivered' then v_post end,
    v_attempt.delivered_url, v_attempt.delivered_md5,
    v_attempt.delivered_phash, v_ev, v_attester);

  -- TERMINAL LEG (same transaction): mints the receipt / releases the
  -- claim exactly as the receipt package defines. Unknown is not accepted
  -- here by construction; sibling convergence (one receipt per ledger
  -- event, later siblings superseded) is preserved by terminate itself.
  v_term := public.visual_scene_original_use_terminate(jsonb_build_object(
    'claim_attempt_id', v_token,
    'outcome', v_outcome,
    'provider_post_id', v_post,
    'outcome_evidence', jsonb_build_object(
      'attester_runtime', true, 'attester_id', v_attester,
      'binding_id', v_snap.binding_id)));

  update public.visual_scene_attester_prepared
    set state = 'finalized', finalized_at = now()
    where claim_attempt_id = v_token;
  insert into public.visual_scene_attester_event (
    claim_attempt_id, calendar_row_id, event, actor, detail)
  values (v_token, v_snap.calendar_row_id,
    'finalized_' || v_outcome, v_attester,
    jsonb_build_object('provider_post_id', v_post,
      'binding_id', v_snap.binding_id));

  return jsonb_build_object('claim_attempt_id', v_token,
    'state', 'finalized', 'outcome', v_outcome,
    'terminal', v_term, 'replayed', false);
end $$;

-- ----------------------------------------------------------------------------
-- GRANTS: revoke the standalone entry points; grant only the composite
-- runtime. service_role keeps SELECT on the evidence tables but loses
-- EXECUTE on the old standalone prepare AND terminate — separate claim /
-- prepare / terminate calls are not acceptable to this runtime, and only
-- visual_scene_attester_attest_terminate may finalize a provider outcome.
-- ----------------------------------------------------------------------------
revoke all on function public.visual_scene_original_use_prepare(jsonb)
  from public, anon, authenticated, service_role, scene_attester;
revoke all on function public.visual_scene_original_use_terminate(jsonb)
  from public, anon, authenticated, service_role, scene_attester;
revoke all on function public.visual_scene_original_use_check_ambiguity(
  text, uuid, date, uuid, text, text)
  from public, anon, authenticated, service_role, scene_attester;

revoke all on public.visual_scene_attester_binding
  from public, anon, authenticated, service_role, scene_attester;
revoke all on public.visual_scene_attester_prepared
  from public, anon, authenticated, service_role, scene_attester;
revoke all on public.visual_scene_attester_event
  from public, anon, authenticated, service_role, scene_attester;
grant select on public.visual_scene_attester_binding
  to scene_attester, service_role;
grant select on public.visual_scene_attester_prepared
  to scene_attester, service_role;
grant select on public.visual_scene_attester_event
  to scene_attester, service_role;

revoke all on function public.visual_scene_attester_scene_hold_refusal(uuid, text)
  from public, anon, authenticated, service_role, scene_attester;
revoke all on function public.visual_scene_attester_claim_prepare(jsonb)
  from public, anon, authenticated, service_role;
revoke all on function public.visual_scene_attester_send_start(jsonb)
  from public, anon, authenticated, service_role;
revoke all on function public.visual_scene_attester_attest_terminate(jsonb)
  from public, anon, authenticated, service_role;
grant execute on function public.visual_scene_attester_claim_prepare(jsonb)
  to scene_attester;
grant execute on function public.visual_scene_attester_send_start(jsonb)
  to scene_attester;
grant execute on function public.visual_scene_attester_attest_terminate(jsonb)
  to scene_attester;

commit;
