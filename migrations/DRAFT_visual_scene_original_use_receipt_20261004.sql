-- DRAFT / UNAPPLIED / OFF. Additive original-use receipt package. Apply
-- strictly after DRAFT_visual_scene_ledger_coverage_gate_20261004.sql (whose
-- whole stack and lock order it preserves). This file grants no production
-- application or activation approval.
--
-- Gap closed (independent review): visual_group_usage_ledger carries NO
-- fingerprint. Its primary key is (gym_id, group_key), so after a media swap
-- the SAME ledger entry is reused by the replacement media while the calendar
-- row id, group and date are unchanged. Nothing in the prior schema proves
-- WHICH bytes the ledger entry originally used, so the ledger gate correctly
-- left every fingerprint-free obligation UNRESOLVED with reason
-- historical_fingerprint_unproven. This draft adds the missing evidence
-- class: an owner-only, immutable, append-only ORIGINAL-USE RECEIPT bound to
-- the original local ledger creation event.
--
-- What a receipt binds (all mandatory, never inferred, never free-form):
--   * the EXACT raw ledger primary key (ledger_gym_id, group_key) plus the
--     canonical tenant/group/date and calendar_row_id — the capture function
--     selects the ledger row by its exact primary key, never by canonical
--     tenant/group with an arbitrary pick;
--   * the row's REAL claim token: claim_attempt_id must equal the referenced
--     content_calendar row's publish_claim_token (the stack's actual claim
--     token), verified in-band — a caller-invented uuid is refused;
--   * exact bytes: source_url + source MD5 and delivered_url + delivered MD5
--     ('md5:'-prefixed, same format as the rest of the stack);
--   * the delivered pHash candidate;
--   * owner byte-read and render lineage as IMMUTABLE FOREIGN KEYS into the
--     owner-created evidence records this stack already has:
--       source_read_receipt / delivered_read_receipt
--         -> public.visual_global_object_read_receipt(receipt_id),
--       render_receipt -> public.visual_global_render_receipt(receipt_id).
--     The capture function verifies each referenced record exists for THIS
--     tenant and matches the claimed url/fingerprint; a render receipt must
--     chain exactly the two read receipts. Free-form JSON/text claims of a
--     byte read, render lineage or provider proof are NOT accepted;
--   * CAUSAL BYTE BINDING (P0 repair): the claim path writes
--     visual_scene_phash_occupied ONLY inside the claim transaction, with
--     the fingerprint/pHash/exact_url derived from ITS OWN scene scan of
--     the candidate bound to the row's delivered object. Capture requires
--     the occupancy row for THIS calendar row, carrying EXACTLY the claimed
--     delivered url/MD5/pHash, to have been written by THIS transaction
--     (epoch-safe: age(xmin) = 0) for the EXACT claimed ledger group, which
--     must equal the calendar row's visual_group_key (the group the claim
--     guard writes occupancy under). Owner-created read/render receipts for
--     unrelated bytes, or an occupancy row written by another claim, can
--     never satisfy this: the claimed bytes must be the bytes THIS claim
--     actually recorded. If the claim's occupancy insert is an idempotent
--     no-op (identical scene+bytes+date already recorded by an earlier
--     claim), capture REFUSES — fail closed; a genuinely NEW ledger entry
--     writes its own occupancy row in the same transaction.
--
-- Removed after independent review (fail closed):
--   * 'historical_verified' capture mode is REMOVED. Historical sends/imports
--     predate the owner-created read/render evidence records this stack
--     defines, so any historical receipt could only be built from unattested
--     free-form text — exactly the fabrication channel the review flagged.
--     Old fingerprint-free ledger entries therefore stay UNRESOLVED
--     (historical_fingerprint_unproven); no historical clearance path exists
--     in this package. If a verifiable historical evidence class is ever
--     defined, it requires a separately reviewed migration.
--   * provider_attempt_id is REMOVED: no provider-attempt evidence record
--     exists in this stack to bind it to, so a free-form provider id only
--     pretended proof. Recording the real provider attempt as evidence is
--     part of the REQUIRED claim-path integration below.
--   * the unique(delivered_url, delivered_md5) CAS key is REMOVED: it wrongly
--     blocked legitimate repeat events using the same delivered object
--     (e.g. a second ledger entry for the same tenant reusing its own
--     delivered object). CAS is on identity (raw ledger key + group + date +
--     calendar row) and on the real claim token only. NOTE: a GLOBAL repeat
--     of the same exact delivered URL by another tenant/group is already
--     blocked upstream — visual_global_object_attestation has exact_url as
--     its PRIMARY KEY, so the same delivered object can never be attested
--     (and hence never receipted) for a second tenant/group.
--
-- P1 repairs (independent Terra review, 2026-10-04):
--   * GROUP BINDING: the claim guard writes visual_scene_phash_occupied with
--     group_key = p_row.visual_group_key (the calendar row's resolved visual
--     group). Capture now requires the same-transaction occupancy row's
--     group_key to equal BOTH the claimed ledger group_key AND the calendar
--     row's visual_group_key; the evaluator's receipt path requires occupancy
--     group_key = receipt.group_key EXACTLY (never scene-linked siblings).
--     A claim whose ledger group differs from the row's visual group, or
--     occupancy recorded for a linked sibling group over the same
--     row/bytes, can never mint or satisfy a receipt — fail closed.
--   * EPOCH-SAFE XID: xmin is a 32-bit xid while txid_current() returns the
--     64-bit epoch-qualified id, so xmin::text::bigint = txid_current() only
--     held in epoch 0 and silently broke after the first xid wrap. Both
--     same-transaction proofs now use age(xmin): PostgreSQL's epoch-aware
--     xid comparison, 0 exactly when the tuple's inserting xid IS this
--     transaction. Assumption (standard xmin discipline): no compared tuple
--     is older than 2^31 transactions. For the ledger check the
--     (gym_id, group_key) primary key makes a second row for the same key
--     impossible, so the alias window is unreachable; for occupancy, a
--     false match additionally requires identical row/bytes/url/date plus a
--     2^32-aligned old xid, and age() rejects every real earlier claim's
--     row (age >= 1), preserving the idempotent-no-op refusal.
--
-- Capture discipline (fail closed): the receipt is written ONLY in the same
-- transaction that created a NEW visual_group_usage_ledger entry. The capture
-- function proves this in-transaction: the fleet scene advisory lock must be
-- held by this backend AND the referenced ledger row — addressed by its exact
-- raw primary key — must have been created by THIS transaction
-- (xmin = txid_current()) with matching identity; the referenced calendar
-- row's real post_date and canonical tenant must equal the claimed
-- date/tenant; and the claim path's OWN occupancy write for that calendar
-- row — exactly the claimed delivered bytes — must have been written by THIS
-- transaction. CAS: identity and claim_attempt_id are unique; any replay
-- raises 23505.
--
-- Resolution rule (additive replacement of
-- visual_scene_ledger_obligation_evaluate, same signature): a
-- fingerprint-free obligation is resolved by a receipt ONLY when the
-- receipt's RAW LEDGER KEY equals the obligation's raw_gym_key, the
-- receipt's stored tenant equals the obligation tenant AND the raw key's
-- CURRENT canonical tenant mapping still equals the obligation tenant (a
-- remapped alias can never clear the new tenant's obligation with a receipt
-- minted under the old mapping), AND the receipt's delivered bytes ALSO tie
-- to ACTUAL recorded scene occupancy BY THAT VERY CLAIM — a
-- visual_scene_phash_occupied row (written only inside a clean claim
-- transaction) whose calendar_row_id equals the receipt's calendar_row_id,
-- for the same tenant/date, whose group is in the obligation's linked
-- scene, whose phash equals the receipt's delivered pHash and whose
-- fingerprint EXACTLY equals the receipt's delivered MD5. A receipt alone, a
-- canonical tenant/group match over a different raw ledger key, an
-- occupancy row written by ANOTHER claim over the same bytes, a surviving
-- published row, current occupancy over different bytes, or a staged
-- candidate are never proof.
--
-- Integration status (fail closed): this draft does NOT modify the frozen
-- claim-wave claim guard or the atomic write/patch drafts, so NEW claims do
-- not yet emit receipts automatically and this package is NOT integrated.
-- Wiring visual_scene_original_use_capture(...) into the claim-success path
-- (same transaction, after the clean-decision occupancy write, while the
-- fleet advisory lock is held, with the row's real publish_claim_token and
-- the owner-created read/render receipt ids) is REQUIRED INTEGRATION and must
-- be separately reviewed/authorized. Until then every fingerprint-free
-- ledger obligation remains unresolved and activation stays refused.
--
-- Preserved lock order (unchanged from the ledger gate):
--   1. content_calendar SHARE ROW EXCLUSIVE write barrier;
--   2. calendar rows FOR UPDATE; component targets collected from the
--      calendar AND all ledgers, locked sorted NOWAIT;
--   3. NOWAIT proof/ledger relation barriers;
--   4. the one fleet-wide visual_scene_global advisory lock, LAST;
--   5. evaluation under the full lock set only (live reread; drift is
--      fail-closed).
-- Receipt writes require the same fleet advisory lock (step 4) that the
-- backfill/activation path holds while evaluating, so a receipt can never be
-- created, replayed or drift while coverage is being evaluated: receipt
-- writers and the coverage evaluator serialize on the advisory lock by
-- construction. The receipt relation itself is append-only and immutable, so
-- there is no update/delete drift channel to re-read. Folding the receipt
-- relation into the gate's NOWAIT relation-barrier list is part of the
-- required claim-path integration above (documented, not silently done).
--
-- The receipt table is owner-only; the diagnostic audit stays read-only and
-- service_role-only exactly as the ledger gate defined.

begin;

-- Refuse an unsafe late install: NO tenant may already be armed, period.
-- Same fail-closed rule as the ledger gate: receipts define activation
-- proof, so they must exist before any arming is possible.
do $$
begin
  if exists(select 1 from public.gym_visual_guard_settings s
      where s.enforce) then
    raise exception 'scene original-use receipt must be installed before any tenant is armed'
      using errcode='55000';
  end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- (a) The original-use receipt. Immutable, append-only, owner-only.
-- One receipt per original local ledger creation event, enforced by the
-- identity unique key on the EXACT RAW LEDGER KEY and the claim-token CAS key.
-- ---------------------------------------------------------------------------
create table if not exists public.visual_scene_original_use_receipt (
  receipt_id        uuid        primary key default gen_random_uuid(),
  capture_mode      text        not null check (capture_mode = 'claim'),
  tenant_id         text        not null,
  group_key         text        not null,
  used_date         date        not null,
  ledger_gym_id     text        not null,
  calendar_row_id   uuid        not null,
  claim_attempt_id  uuid        not null,
  source_url        text        not null check (source_url ~ '^https://'),
  source_md5        text        not null check (source_md5 ~ '^md5:[0-9a-f]{32}$'),
  delivered_url     text        not null check (delivered_url ~ '^https://'),
  delivered_md5     text        not null check (delivered_md5 ~ '^md5:[0-9a-f]{32}$'),
  delivered_phash   char(16)    not null check (delivered_phash ~ '^[0-9a-f]{16}$'),
  source_read_receipt    uuid   not null
      references public.visual_global_object_read_receipt (receipt_id),
  delivered_read_receipt uuid   not null
      references public.visual_global_object_read_receipt (receipt_id),
  render_receipt    uuid
      references public.visual_global_render_receipt (receipt_id),
  captured_txid     bigint      not null default txid_current(),
  captured_at       timestamptz not null default now(),
  foreign key (ledger_gym_id, group_key)
      references public.visual_group_usage_ledger (gym_id, group_key),
  foreign key (calendar_row_id) references public.content_calendar (id),
  -- One receipt per original ledger creation event (EXACT RAW identity).
  unique (ledger_gym_id, group_key, used_date, calendar_row_id),
  -- CAS: a real claim token is consumed exactly once.
  unique (claim_attempt_id),
  -- A render receipt exists exactly when the delivered bytes differ from the
  -- source bytes (a real render/reburn/rehost happened).
  check (((source_url, source_md5) = (delivered_url, delivered_md5))
         = (render_receipt is null))
);

comment on table public.visual_scene_original_use_receipt is
  'DRAFT/UNAPPLIED/OFF: owner-only immutable append-only original-use receipt bound to the exact raw primary key of the original visual_group_usage_ledger creation event. Claim-mode only: captured in the same transaction as a NEW claim, bound to the row''s real publish_claim_token and to owner-created read/render evidence records by immutable FK. No historical mode, no free-form attestation, no provider-claim field. Old fingerprint-free ledger entries without receipts stay UNRESOLVED.';

create index if not exists visual_scene_original_use_receipt_tenant_date_idx
  on public.visual_scene_original_use_receipt (tenant_id, used_date);

-- Immutability: no update, delete or truncate, ever.
create or replace function public.visual_scene_original_use_receipt_immutable()
returns trigger language plpgsql set search_path = public as $$
begin
  raise exception 'visual scene original-use receipts are append-only and immutable'
    using errcode='23514';
end;
$$;

drop trigger if exists visual_scene_original_use_receipt_immutable
  on public.visual_scene_original_use_receipt;
create trigger visual_scene_original_use_receipt_immutable
  before update or delete or truncate on public.visual_scene_original_use_receipt
  for each statement execute function public.visual_scene_original_use_receipt_immutable();

-- ---------------------------------------------------------------------------
-- (b) Capture. Owner-only. Proves the lock, exact ledger PK, real claim
-- token and immutable evidence binding in-band instead of trusting the
-- caller.
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_original_use_capture(
  p jsonb
) returns uuid language plpgsql security definer set search_path = public as $$
declare
  v_mode text := p->>'capture_mode';
  v_tenant text := p->>'tenant_id';
  v_group text := p->>'group_key';
  v_date date := nullif(p->>'used_date','')::date;
  v_ledger_gym text := p->>'ledger_gym_id';
  v_row_id uuid := nullif(p->>'calendar_row_id','')::uuid;
  v_claim_attempt uuid := nullif(p->>'claim_attempt_id','')::uuid;
  v_source_url text := p->>'source_url';
  v_source_md5 text := p->>'source_md5';
  v_delivered_url text := p->>'delivered_url';
  v_delivered_md5 text := p->>'delivered_md5';
  v_phash text := p->>'delivered_phash';
  v_src_rr uuid := nullif(p->>'source_read_receipt','')::uuid;
  v_del_rr uuid := nullif(p->>'delivered_read_receipt','')::uuid;
  v_render_rr uuid := nullif(p->>'render_receipt','')::uuid;
  v_ledger public.visual_group_usage_ledger%rowtype;
  v_row public.content_calendar%rowtype;
  v_receipt uuid;
begin
  -- Claim-mode only. There is no historical path: historical evidence cannot
  -- be bound to owner-created read/render records, so it is refused outright.
  if v_mode is distinct from 'claim' then
    raise exception 'original-use receipt capture_mode must be claim (no historical path exists)'
      using errcode='23514';
  end if;
  -- Every evidence field is mandatory; bytes are never inferred.
  if v_tenant is null or v_group is null or v_date is null
      or v_ledger_gym is null or v_row_id is null or v_claim_attempt is null
      or v_source_url is null or v_source_md5 is null
      or v_delivered_url is null or v_delivered_md5 is null or v_phash is null
      or v_src_rr is null or v_del_rr is null then
    raise exception 'original-use receipt requires complete identity, claim token, byte and evidence-FK fields'
      using errcode='23514';
  end if;

  -- The one fleet-wide scene advisory lock must already be held by THIS
  -- backend: a receipt is written only inside a claim transaction holding
  -- the same lock the coverage evaluator holds last.
  if not exists(select 1 from pg_locks l
      where l.pid=pg_backend_pid() and l.locktype='advisory' and l.granted
        and l.classid=hashtextextended(
          jsonb_build_array('visual_scene_global')::text,0)>>32
        and l.objid=hashtextextended(
          jsonb_build_array('visual_scene_global')::text,0)&4294967295) then
    raise exception 'original-use receipt capture requires the fleet scene advisory lock'
      using errcode='25006';
  end if;

  -- The referenced ledger row is addressed by its EXACT RAW PRIMARY KEY —
  -- never picked by canonical tenant/group. Canonical tenant mapping is then
  -- verified against the same mapping the obligations view uses.
  select l.* into v_ledger from public.visual_group_usage_ledger l
    where l.gym_id=v_ledger_gym and l.group_key=v_group;
  if not found
      or public.visual_group_tenant_id(v_ledger.gym_id)::text is distinct from v_tenant
      or v_ledger.reserved_date is distinct from v_date
      or v_ledger.calendar_row_id is distinct from v_row_id then
    raise exception 'original-use receipt identity does not match the exact ledger row'
      using errcode='23514';
  end if;

  -- Same transaction as the NEW claim: the ledger row must have been created
  -- by THIS transaction. Epoch-safe: age(xmin) = 0 exactly when xmin is this
  -- transaction's xid (xmin::text::bigint vs txid_current() compared a 32-bit
  -- xid with a 64-bit epoch-qualified id and broke after the first wrap). A
  -- pre-existing ledger row can never mint a receipt.
  if age(v_ledger.xmin) <> 0 then
    raise exception 'claim receipt must be captured in the same transaction as the new ledger entry'
      using errcode='25006';
  end if;

  -- The claim token must be the REAL one on the referenced calendar row, not
  -- a caller-invented value.
  select c.* into v_row from public.content_calendar c where c.id=v_row_id;
  if not found then
    raise exception 'original-use receipt requires an existing calendar row'
      using errcode='23503';
  end if;
  if v_row.publish_claim_token is null
      or v_row.publish_claim_token is distinct from v_claim_attempt then
    raise exception 'original-use receipt claim_attempt_id must equal the calendar row publish_claim_token'
      using errcode='23514';
  end if;

  -- The calendar row itself must agree with the claimed identity: its real
  -- post date and its canonical tenant mapping must equal the claimed
  -- date/tenant — one event, not three unrelated rows.
  if v_row.post_date is distinct from v_date
      or public.visual_group_tenant_id(v_row.gym_id)::text
         is distinct from v_tenant then
    raise exception 'original-use receipt calendar row identity does not match the claimed tenant/date'
      using errcode='23514';
  end if;

  -- CAUSAL BYTE BINDING (P0 repair): the claimed delivered bytes must be
  -- the bytes THIS claim actually used. visual_scene_phash_occupied is
  -- written ONLY by the claim path, inside the claim transaction, with the
  -- fingerprint/pHash/exact_url from ITS OWN scene scan of the candidate
  -- bound to this row's delivered object. Require the occupancy row for
  -- THIS calendar row, carrying EXACTLY the claimed delivered
  -- url/MD5/pHash, to have been written by THIS transaction (epoch-safe:
  -- age(xmin) = 0), under the EXACT claimed ledger group — which the claim
  -- guard derives from the row's resolved visual_group_key, so the row's
  -- visual_group_key must equal the claimed ledger group too. Occupancy
  -- recorded for a scene-LINKED SIBLING group over the same row/bytes, an
  -- occupancy row written by another claim (or in an earlier transaction),
  -- or owner-created read/render receipts for unrelated bytes can never
  -- satisfy this. If the claim's occupancy insert was an idempotent no-op
  -- (same scene+bytes+date already recorded by an earlier claim), capture
  -- REFUSES — fail closed.
  if not exists(select 1 from public.visual_scene_phash_occupied o
      where o.calendar_row_id=v_row_id and o.tenant_id=v_tenant
        and o.used_date=v_date
        and o.group_key=v_group and o.group_key=v_row.visual_group_key
        and o.phash=v_phash::char(16) and o.fingerprint=v_delivered_md5
        and o.evidence->>'exact_url'=v_delivered_url
        and age(o.xmin) = 0) then
    raise exception 'original-use receipt delivered bytes were not recorded by this claim transaction'
      using errcode='23514';
  end if;

  -- Byte evidence is bound by immutable FK, verified in-band: the read
  -- receipts must be owner-created records for THIS tenant matching the
  -- claimed urls/fingerprints exactly; a render receipt must chain exactly
  -- the two read receipts. Free-form claims are not accepted.
  if not exists(select 1 from public.visual_global_object_read_receipt r
      where r.receipt_id=v_src_rr and r.tenant_id=v_tenant
        and r.exact_url=v_source_url and r.fingerprint=v_source_md5) then
    raise exception 'original-use receipt source bytes are not bound to an owner read receipt'
      using errcode='23503';
  end if;
  if not exists(select 1 from public.visual_global_object_read_receipt r
      where r.receipt_id=v_del_rr and r.tenant_id=v_tenant
        and r.exact_url=v_delivered_url and r.fingerprint=v_delivered_md5) then
    raise exception 'original-use receipt delivered bytes are not bound to an owner read receipt'
      using errcode='23503';
  end if;
  if (v_source_url, v_source_md5) is distinct from (v_delivered_url, v_delivered_md5) then
    if v_render_rr is null or not exists(
        select 1 from public.visual_global_render_receipt r
        where r.receipt_id=v_render_rr and r.tenant_id=v_tenant
          and r.source_read_receipt=v_src_rr and r.delivered_read_receipt=v_del_rr
          and r.source_exact_url=v_source_url and r.delivered_exact_url=v_delivered_url
          and r.source_fingerprint=v_source_md5 and r.delivered_fingerprint=v_delivered_md5) then
      raise exception 'original-use receipt render lineage is not bound to a chaining owner render receipt'
        using errcode='23503';
    end if;
  elsif v_render_rr is not null then
    raise exception 'original-use receipt render receipt is only valid when delivered bytes differ from source bytes'
      using errcode='23514';
  end if;

  -- CAS refusal is explicit (the unique keys back it with 23505 on race).
  if exists(select 1 from public.visual_scene_original_use_receipt r
      where (r.ledger_gym_id,r.group_key,r.used_date,r.calendar_row_id)
            = (v_ledger_gym,v_group,v_date,v_row_id)
        or r.claim_attempt_id=v_claim_attempt) then
    raise exception 'original-use receipt already exists for this identity or claim token'
      using errcode='23505';
  end if;

  insert into public.visual_scene_original_use_receipt (
    capture_mode, tenant_id, group_key, used_date, ledger_gym_id,
    calendar_row_id, claim_attempt_id,
    source_url, source_md5, delivered_url, delivered_md5,
    delivered_phash,
    source_read_receipt, delivered_read_receipt, render_receipt
  ) values (
    'claim', v_tenant, v_group, v_date, v_ledger.gym_id,
    v_row_id, v_claim_attempt,
    v_source_url, v_source_md5, v_delivered_url, v_delivered_md5,
    v_phash::char(16),
    v_src_rr, v_del_rr, v_render_rr
  ) returning receipt_id into v_receipt;
  return v_receipt;
end;
$$;

-- ---------------------------------------------------------------------------
-- (c) Additive replacement of the ledger obligation evaluator (same
-- signature, same lock/write discipline: writes and locks nothing). Only
-- change vs the ledger gate: a fingerprint-free obligation can now be
-- resolved by an immutable original-use receipt whose RAW LEDGER KEY equals
-- the obligation's raw_gym_key, whose stored tenant AND the raw key's
-- CURRENT canonical mapping equal the obligation tenant, and whose
-- delivered bytes tie to ACTUAL recorded scene occupancy BY THE BOUND CLAIM
-- ITSELF (occupancy calendar_row_id = receipt's calendar_row_id AND
-- occupancy group_key = receipt's claimed ledger group EXACTLY — never a
-- scene-linked sibling).
-- Everything else is byte-identical, and receipt-free old entries stay
-- unresolved exactly as before.
-- ---------------------------------------------------------------------------
create or replace function public.visual_scene_ledger_obligation_evaluate(
  p_obligation jsonb,
  out o_status text, out o_reason text,
  out o_tenant text, out o_group_key text, out o_used_date date,
  out o_phash char(16), out o_occupied_fingerprint text,
  out o_calendar_row_id uuid
) language plpgsql stable security definer set search_path = public as $$
declare
  v_row public.content_calendar%rowtype;
  v_eval record;
  v_occ record;
  v_receipt record;
  v_fp text := p_obligation->>'fingerprint';
  v_grp text := p_obligation->>'group_key';
  v_raw text := p_obligation->>'raw_gym_key';
begin
  o_status := 'unresolved';
  o_tenant := p_obligation->>'tenant_id';
  o_group_key := v_grp;
  o_used_date := nullif(p_obligation->>'used_date','')::date;
  o_calendar_row_id := nullif(p_obligation->>'calendar_row_id','')::uuid;

  -- A NULL date is never inferred from timestamps, siblings or receipts.
  if o_used_date is null then
    o_reason := 'missing_date'; return;
  end if;
  if o_tenant is null then
    o_reason := 'unmapped_tenant'; return;
  end if;

  if v_fp is null then
    -- Fingerprint-free obligation (per-gym usage ledger). ONLY an immutable
    -- original-use receipt bound to this obligation's EXACT RAW LEDGER KEY
    -- can prove WHICH bytes it used, and only when the raw key's CURRENT
    -- canonical mapping still equals the obligation tenant AND the receipt's
    -- delivered bytes also tie to ACTUAL recorded scene occupancy BY THE
    -- BOUND CLAIM ITSELF (occupancy calendar_row_id = receipt's). A receipt
    -- alone, a canonical tenant/group match over a different raw key, a
    -- stale/remapped alias mapping, an unrelated occupancy row over the
    -- same bytes, a surviving published row, current occupancy over
    -- different bytes (the post-swap replacement), or a staged candidate
    -- are never proof.
    if v_raw is null then
      o_reason := 'historical_fingerprint_unproven'; return;
    end if;
    select r.* into v_receipt
      from public.visual_scene_original_use_receipt r
      where r.ledger_gym_id=v_raw and r.group_key=v_grp
        and r.tenant_id=o_tenant
        -- The raw key's CURRENT canonical mapping must still equal the
        -- obligation tenant: a receipt minted under an old mapping can
        -- never falsely clear another tenant/alias's obligation after a
        -- remap.
        and public.visual_group_tenant_id(r.ledger_gym_id)::text=o_tenant
        and r.used_date=o_used_date
        and (o_calendar_row_id is null or r.calendar_row_id=o_calendar_row_id)
      order by r.captured_at, r.receipt_id limit 1;
    if not found then
      o_reason := 'historical_fingerprint_unproven'; return;
    end if;
    select o.phash, o.fingerprint into v_occ
      from public.visual_scene_phash_occupied o
      where o.tenant_id=o_tenant and o.used_date=o_used_date
        -- The occupancy must be the VERY ROW the bound claim wrote for this
        -- calendar row under the receipt's EXACT claimed ledger group (the
        -- group capture proved equals the row's visual_group_key): an
        -- unrelated occupancy over the same bytes — another claim, another
        -- calendar row, or a scene-LINKED SIBLING group — is never proof.
        and o.calendar_row_id=v_receipt.calendar_row_id
        and o.group_key=v_receipt.group_key
        and o.phash=v_receipt.delivered_phash
        and o.fingerprint=v_receipt.delivered_md5
      order by o.phash limit 1;
    if not found then
      -- The receipt exists but its exact delivered bytes were never
      -- recorded as scene occupancy BY THE BOUND CLAIM (e.g. occupancy
      -- covers another claim's row or the NEW media only). Fail closed.
      o_reason := 'original_use_receipt_occupancy_unproven'; return;
    end if;
    o_status := 'resolved'; o_reason := 'original_use_receipt';
    o_phash := v_occ.phash; o_occupied_fingerprint := v_occ.fingerprint;
    return;
  end if;

  -- Path 1: the obligation references a surviving published calendar row.
  -- Reuse the exact row proof, then require identity agreement with the
  -- obligation (tenant/date/linked group/fingerprint). A same group/date
  -- match alone is never proof.
  if o_calendar_row_id is not null then
    select c.* into v_row from public.content_calendar c
      where c.id=o_calendar_row_id;
    if found and (v_row.status='published' or v_row.published_at is not null) then
      select * into v_eval from public.visual_scene_history_evaluate(v_row);
      if v_eval.o_status <> 'ready' then
        o_reason := 'calendar_row_'||v_eval.o_reason; return;
      end if;
      if v_eval.o_tenant = o_tenant and v_eval.o_post_date = o_used_date
          and (v_grp is null or v_eval.o_group_key in (
            select sm.group_key from
              public.visual_group_scene_members(o_tenant,v_grp) sm(group_key)))
          and v_eval.o_fingerprint = v_fp then
        o_status := 'resolved'; o_reason := 'calendar_row_proof';
        o_phash := v_eval.o_phash; o_occupied_fingerprint := v_eval.o_fingerprint;
      else
        o_reason := 'calendar_row_identity_mismatch';
      end if;
      return;
    end if;
  end if;

  -- Path 2: retained historical identity tied to ACTUAL scene occupancy. The
  -- occupied row was written only inside a clean claim transaction, so it is
  -- the durable occupancy record. A staged candidate, the current
  -- replacement object, or a same group/date row over different bytes never
  -- satisfies this proof.
  if v_grp is not null then
    select o.phash, o.fingerprint into v_occ
      from public.visual_scene_phash_occupied o
      where o.tenant_id=o_tenant and o.used_date=o_used_date
        and o.group_key in (select sm.group_key from
          public.visual_group_scene_members(o_tenant,v_grp) sm(group_key))
        and o.fingerprint=v_fp
      order by o.phash limit 1;
  else
    -- Orphan/global owners carry no group: the exact fingerprint itself must
    -- have recorded occupancy for this tenant on this date.
    select o.phash, o.fingerprint into v_occ
      from public.visual_scene_phash_occupied o
      where o.tenant_id=o_tenant and o.used_date=o_used_date
        and o.fingerprint=v_fp
      order by o.phash limit 1;
  end if;
  if not found then
    if exists(
        select 1 from public.visual_scene_phash_occupied o
        where o.tenant_id=o_tenant and o.used_date=o_used_date
          and (v_grp is null or o.group_key in (select sm.group_key from
            public.visual_group_scene_members(o_tenant,v_grp) sm(group_key)))) then
      o_reason := 'fingerprint_mismatch';
    else
      o_reason := 'no_scene_occupancy_proof';
    end if;
    return;
  end if;
  o_status := 'resolved'; o_reason := 'scene_occupancy_proof';
  o_phash := v_occ.phash; o_occupied_fingerprint := v_occ.fingerprint;
end;
$$;

-- Owner-only mutation surface; receipts are not readable by non-owners
-- beyond the existing service_role ledger coverage audit.
revoke all on function public.visual_scene_original_use_capture(jsonb)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_scene_original_use_receipt_immutable()
  from public,anon,authenticated,service_role;
revoke all on public.visual_scene_original_use_receipt
  from public,anon,authenticated,service_role;
grant select on public.visual_scene_original_use_receipt to service_role;

commit;
