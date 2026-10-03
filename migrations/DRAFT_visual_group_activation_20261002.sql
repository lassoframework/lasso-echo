-- DRAFT / UNAPPLIED. Transactional service-role activation for the visual
-- guard. Apply schema, claim trigger and backfill drafts, then the global
-- history draft, and apply this activation draft LAST. This file
-- grants NO production application or activation approval; the global ledger
-- stays DRAFT/OFF until an owner ruling.
-- Rollback: set enforce=false for any armed tenant (disarming is always
-- permitted), drop trigger tenant_alias_arm_guard, restore the schema
-- migration's visual_group_settings_arm_guard definition, drop
-- visual_group_activate_guard and visual_group_activation. Preserve permanent
-- published usage/history; DROP is not a data rollback after live activation.
-- Never remove calendar claim/release integration or global history while any
-- tenant is armed; preserve all global published rows during rollback.
--
-- Barrier contract (all in ONE fresh READ COMMITTED transaction):
--   1. LOCK TABLE content_calendar IN SHARE ROW EXCLUSIVE MODE first, before
--      any advisory lock, row lock or calendar read. SHARE ROW EXCLUSIVE
--      conflicts with the ROW EXCLUSIVE of every calendar INSERT/UPDATE/
--      DELETE (including claim-trigger writers). FOR UPDATE readers may
--      remain; their auxiliary tenant/key locks are TRYed below, so we
--      release/refuse instead of waiting for their blocked calendar writes.
--   2. TRY one canonical tenant auxiliary advisory, then TRY per-key
--      backfill advisories in sorted order. Contention raises 55P03; never
--      wait for an advisory owner that might be blocked by our barrier.
--   3. Real backfill for EVERY covered calendar alias key, then a locked
--      re-read of all calendar rows (any status/variant: archived, held,
--      candidate, published, pending), ledger, siblings, events, scene
--      components and holds.
--   4. Refusal raises: the whole transaction (alias registrations, backfill
--      writes, receipt) rolls back and enforce stays OFF.
--   5. Only a same-transaction activation receipt authorizes the enforce=true
--      write; direct settings arming without it is denied by the arm guard.

-- ---------------------------------------------------------------------------
-- 1. Activation receipts: same-transaction proof that coverage was verified
-- ---------------------------------------------------------------------------
create table if not exists public.visual_group_activation (
  gym_id         text        primary key,  -- canonical tenant UUID text
  proof          jsonb       not null,
  actor          text        not null default 'system',
  transaction_id bigint      not null default txid_current(),
  created_at     timestamptz not null default now()
);

-- The global draft installs a temporary arming blocker. Replace it only when
-- every global object used by the integrated calendar transaction exists.
do $$ begin
  if to_regclass('public.visual_global_identity') is null
     or to_regclass('public.visual_global_usage') is null
     or to_regclass('public.visual_global_object_attestation') is null
     or to_regclass('public.visual_global_scene_object_member') is null
     or to_regclass('public.visual_global_object_lineage') is null
     or to_regprocedure('public.visual_global_claim_scene(public.content_calendar,boolean,boolean)') is null
     or to_regprocedure('public.visual_global_claim_fingerprint_set(text,text,date,uuid,text,boolean,boolean,text[])') is null
     or to_regprocedure('public.visual_global_refresh_scene_history(text,text)') is null
     or to_regprocedure('public.visual_global_import_history()') is null
     or to_regprocedure('public.visual_global_row_bytes_verified(public.content_calendar)') is null
     or to_regprocedure('public.visual_global_release(text,text)') is null then
    raise exception 'apply global visual history before integrated activation' using errcode='55000';
  end if;
  if not exists(select 1 from pg_trigger
      where tgrelid='public.visual_group_scene_link'::regclass
        and tgname='visual_global_scene_link_claim_guard'
        and not tgisinternal and tgenabled<>'D') then
    raise exception 'global scene-link claim guard is missing before integrated activation'
      using errcode='55000';
  end if;
end $$;
drop trigger if exists visual_global_block_local_activation
  on public.gym_visual_guard_settings;

comment on table public.visual_group_activation is
  'Arming receipts. The activation RPC writes one row per canonical tenant in the same transaction as the enforce=true write; the settings arm guard requires a receipt with the CURRENT txid, so direct/manual arming is impossible.';

alter table public.visual_group_activation enable row level security;
drop policy if exists visual_group_activation_service_read on public.visual_group_activation;
create policy visual_group_activation_service_read on public.visual_group_activation
  for select to service_role using (true);
revoke all on public.visual_group_activation from public,anon,authenticated,service_role;
-- Receipts are written only by visual_group_activate_guard (SECURITY DEFINER).
grant select on public.visual_group_activation to service_role;

-- ---------------------------------------------------------------------------
-- 2. Arm guard: arming now requires a same-transaction activation receipt
-- ---------------------------------------------------------------------------
-- Replaces the schema draft's definition (canonical-mapping checks kept
-- verbatim). Disarming (enforce=false) never requires a receipt.
create or replace function public.visual_group_settings_arm_guard()
returns trigger language plpgsql set search_path = public as $$
begin
  -- Armed rows must be keyed by the canonical tenant UUID itself; a raw alias
  -- key or an unmapped key can never arm the guard.
  if new.enforce and (public.visual_group_tenant_id(new.gym_id) is null
      or new.gym_id <> public.visual_group_tenant_id(new.gym_id)::text) then
    raise exception 'unmapped calendar key cannot arm the visual guard' using errcode = '23514';
  end if;
  -- Coverage proof: only the transactional activation RPC, in its current
  -- transaction, may arm a tenant. A receipt from an older transaction, a
  -- forged row (direct writes are revoked) or a user-set GUC cannot arm.
  if new.enforce and not exists(
    select 1 from public.visual_group_activation a
    where a.gym_id = new.gym_id and a.transaction_id = txid_current()
  ) then
    raise exception 'arming the visual guard requires the transactional activation RPC with fresh coverage proof'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

-- ---------------------------------------------------------------------------
-- 3. Fresh calendar alias keys cannot appear under an armed tenant
-- ---------------------------------------------------------------------------
-- Coverage is proven against the alias keys visible under the barrier. A new
-- raw key registered after arming would resolve to the armed tenant yet its
-- rows would have no backfill/ledger proof, and the claim trigger's strict
-- resolver would previously have been the only (read-time) line of defense.
-- Fail closed: new keys for an armed tenant require disarm + reactivation.
create or replace function public.visual_group_tenant_alias_arm_guard()
returns trigger language plpgsql set search_path = public as $$
begin
  -- Re-registration of an existing key is a no-op/conflict path; only a
  -- genuinely NEW key under an armed tenant is refused.
  if exists(select 1 from public.tenant_alias t where t.alias_key = btrim(new.alias_key)) then
    return new;
  end if;
  if exists(select 1 from public.gym_visual_guard_settings s
    where s.gym_id = new.tenant_id::text and s.enforce) then
    raise exception 'tenant is armed; a new calendar alias key requires disarm and reactivation with fresh coverage proof'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

drop trigger if exists tenant_alias_arm_guard on public.tenant_alias;
create trigger tenant_alias_arm_guard before insert on public.tenant_alias
  for each row execute function public.visual_group_tenant_alias_arm_guard();

-- ---------------------------------------------------------------------------
-- 4. Transactional activation RPC (service-role only)
-- ---------------------------------------------------------------------------
create or replace function public.visual_group_activate_guard(
  p_gym_id text, p_actor text default null
) returns jsonb language plpgsql security definer set search_path = public as $$
declare
  v_tenant uuid;
  v_tenant_text text;
  v_actor text := coalesce(nullif(btrim(p_actor), ''), 'system');
  v_keys text[];
  v_key text;
  v_bf jsonb;
  v_backfills jsonb := '[]'::jsonb;
  v_calendar_rows integer;
  v_ledger_rows integer;
  v_sibling_rows integer;
  v_global_rows integer;
  v_proof jsonb;
begin
  if nullif(btrim(p_gym_id), '') is null then
    raise exception 'gym key required' using errcode = '22023';
  end if;
  -- Caller must run this in a FRESH READ COMMITTED transaction: the barrier
  -- lock and every re-read below must see newly committed writer state.
  if current_setting('transaction_isolation') <> 'read committed' then
    raise exception 'activation requires a fresh READ COMMITTED transaction' using errcode = '25006';
  end if;
  -- Fail closed on unmapped keys: they can never be armed.
  v_tenant := public.visual_group_tenant_strict(p_gym_id);
  v_tenant_text := v_tenant::text;

  -- Idempotent read path: already armed tenants need no barrier or writes.
  if exists(select 1 from public.gym_visual_guard_settings s
    where s.gym_id = v_tenant_text and s.enforce) then
    return jsonb_build_object('tenant', v_tenant_text, 'actor', v_actor,
      'enforced', true, 'idempotent', true,
      'note', 'already armed; no barrier or coverage recheck performed');
  end if;

  -- A reused transaction may already own an advisory/calendar row lock,
  -- allowing a barrier upgrade to deadlock before our TRY-lock can run. Refuse
  -- that observable non-fresh state. Plain prior reads do not weaken READ
  -- COMMITTED's fresh snapshots and are compatible with the write barrier.
  if exists(select 1 from pg_locks where pid=pg_backend_pid() and granted and
      (locktype='advisory' or (locktype='relation'
        and relation='public.content_calendar'::regclass and mode<>'AccessShareLock'))) then
    raise exception 'activation requires a fresh transaction without prior advisory or calendar write/row locks'
      using errcode='25006';
  end if;

  -- 1. WRITE BARRIER FIRST: drains and blocks all calendar writers (ROW
  -- EXCLUSIVE conflicts) before any advisory lock, row lock or calendar read.
  lock table public.content_calendar in share row exclusive mode;
  -- Freeze cross-tenant history/identity inputs as well. NOWAIT avoids a
  -- deadlock if an auxiliary writer owns one while waiting for our calendar
  -- barrier; a retry must start a new transaction.
  lock table public.visual_group_usage_ledger, public.visual_group_alias,
    public.visual_global_identity, public.visual_group_scene_link,
    public.visual_group_usage_sibling, public.visual_group_member_event,
    public.visual_group_reconciliation, public.tenant_alias,
    public.visual_global_object_attestation,
    public.visual_global_scene_object_member, public.visual_global_object_lineage,
    public.visual_global_usage, public.visual_global_usage_member
    in share row exclusive mode nowait;

  -- 2. The SAME canonical mutex every auxiliary mutation RPC takes before
  -- its other locks. Never wait after the barrier: an auxiliary writer may
  -- own this lock while queued for calendar DML that our barrier blocks.
  if not pg_try_advisory_xact_lock(hashtextextended(
    jsonb_build_array('visual_tenant',v_tenant_text)::text,0)) then
    raise exception 'activation refused: canonical tenant auxiliary writer busy; retry transaction'
      using errcode='55P03';
  end if;

  -- Every raw calendar key that resolves to this canonical tenant, plus the
  -- key the caller used. Keys of other tenants are never touched.
  select coalesce(array_agg(k order by k), '{}') into v_keys from (
    select distinct c.gym_id as k from public.content_calendar c
      where public.visual_group_tenant_id(c.gym_id) = v_tenant
    union select btrim(p_gym_id)
  ) keys(k);

  for v_key in select unnest(v_keys) order by 1 loop
    if not pg_try_advisory_xact_lock(hashtextextended(
      jsonb_build_array('visual_backfill',v_key)::text,0)) then
      raise exception 'activation refused: backfill key busy; retry transaction' using errcode='55P03';
    end if;
  end loop;

  -- 3a. Register/verify every covered calendar key against this tenant.
  if exists(select 1 from unnest(v_keys) k
    where not exists(select 1 from public.tenant_alias t
      where t.alias_key = k and t.tenant_id = v_tenant)) then
    raise exception 'activation refused: unmapped or foreign tenant calendar key under barrier'
      using errcode = '23514';
  end if;

  -- Publication time is not a verified gym calendar date (UTC can differ
  -- from the tenant's local date). Never arm undated published history using
  -- a timestamp fallback, even if a previous ledger inferred that date.
  if exists(select 1 from public.content_calendar r
    where r.gym_id=any(v_keys) and (r.status='published' or r.published_at is not null)
      and r.post_date is null) then
    raise exception 'activation refused: undated published row has no verified calendar post_date'
      using errcode = '23514';
  end if;

  -- BEFORE any backfill registration: existing exact delivered identity is
  -- required for every published/ambiguous/active row, including HELD unknown
  -- rows. Active held review decisions are never cleared by preflight. A
  -- backfill cannot manufacture the evidence used to authorize activation.
  -- Captured unresolved attempts remain authoritative even if status markers
  -- suppress the stateless ambiguity helper. Held rows are never unheld here.
  if exists(select 1 from public.content_calendar r
    where r.gym_id=any(v_keys)
      and (r.status='published' or r.published_at is not null
        or public.visual_group_row_ambiguous(r) or public.visual_group_row_active(r))
      and (public.visual_group_resolve_row(r) is null
        or ((r.status='published' or r.published_at is not null
          or public.visual_group_row_ambiguous(r)
          or (public.visual_group_row_active(r) and r.media_not_ready_reason is null))
          and public.visual_group_row_review_pending(r)))) then
    raise exception 'activation refused: unknown, ambiguous or review-pending media identity before backfill'
      using errcode = '23514';
  end if;

  -- Legacy generic hashes can collapse source and derived byte identities.
  -- They remain visible for repair but cannot enter an armed authority.
  if exists(select 1 from public.visual_group_alias a
      where a.gym_id=v_tenant_text and a.alias_kind='byte_hash'
        and a.alias_value !~ '^(source|derived):(sha256:[0-9a-f]{64}|md5:[0-9a-f]{32})$')
     or exists(select 1 from public.content_calendar r
      where r.gym_id=any(v_keys) and nullif(btrim(r.byte_hash),'') is not null
        and lower(btrim(r.byte_hash)) !~
          '^(source|derived):(sha256:[0-9a-f]{64}|md5:[0-9a-f]{32})$') then
    raise exception 'activation refused: legacy byte_hash lacks source/derived algorithm namespace'
      using errcode='23514';
  end if;

  -- 3b. ACTUAL backfill for every covered alias key (not just one). Held rows
  -- become review events; the locked re-read below refuses on any of them, so
  -- a refusal rolls back ALL of these writes with the rest of the transaction.
  for v_key in select unnest(v_keys) order by 1 loop
    v_bf := public.visual_group_backfill_gym(v_key, false);
    v_backfills := v_backfills || jsonb_build_object('key', v_key, 'report', v_bf);
  end loop;

  -- 3c. Locked re-read of ALL calendar rows for every covered key (any
  -- status/variant, including archived, held, candidate, published, pending),
  -- plus ledger, siblings, decision events, scene components and holds.

  -- Unknown, ambiguous or review-pending media identity on any
  -- authority-bearing row (published, active or ambiguous attempt).
  if exists(select 1 from public.content_calendar r
    where r.gym_id = any(v_keys)
      and (r.status = 'published' or r.published_at is not null
        or public.visual_group_row_active(r) or public.visual_group_row_ambiguous(r))
      and (public.visual_group_resolve_row(r) is null
        or public.visual_group_row_review_pending(r))) then
    raise exception 'activation refused: unknown, ambiguous or review-pending media identity'
      using errcode = '23514';
  end if;

  -- Permanent published usage must exist for every published row's group.
  if exists(select 1 from public.content_calendar r
    where r.gym_id = any(v_keys)
      and (r.status = 'published' or r.published_at is not null)
      and not exists(select 1 from public.visual_group_usage_ledger l
        where l.gym_id = v_tenant_text
          and l.group_key = public.visual_group_resolve_row(r)
          and l.state = 'published'
          and r.post_date is not null and l.reserved_date = r.post_date)) then
    raise exception 'activation refused: published row missing permanent published usage ledger coverage or date parity'
      using errcode = '23514';
  end if;

  -- Active unheld rows need a live dated reservation AND an active sibling.
  if exists(select 1 from public.content_calendar r
    where r.gym_id = any(v_keys)
      and public.visual_group_row_active(r)
      and r.media_not_ready_reason is null
      and (r.post_date is null
        or not exists(select 1 from public.visual_group_usage_ledger l
          where l.gym_id = v_tenant_text
            and l.group_key = public.visual_group_resolve_row(r)
            and l.state <> 'released' and l.reserved_date = r.post_date)
        or not exists(select 1 from public.visual_group_usage_sibling s
          where s.gym_id = v_tenant_text
            and s.group_key = public.visual_group_resolve_row(r)
            and s.calendar_row_id = r.id and s.state = 'active'))) then
    raise exception 'activation refused: active row missing dated ledger reservation or active sibling coverage'
      using errcode = '23514';
  end if;

  -- No unresolved historical review holds (any actor), unless a newer
  -- confirm/reject of the same identity or a reconciliation receipt closed it.
  if exists(select 1 from public.visual_group_member_event e
    where e.gym_id = v_tenant_text and e.action = 'review_hold'
      and not exists(select 1 from public.visual_group_reconciliation rc
        where rc.gym_id = e.gym_id and e.id = any(rc.hold_event_ids))
      and not exists(select 1 from public.visual_group_member_event n
        where n.gym_id = e.gym_id
          and n.alias_kind is not distinct from e.alias_kind
          and n.alias_value = e.alias_value and n.id > e.id
          and n.action in ('confirmed','rejected'))) then
    raise exception 'activation refused: unresolved historical review events'
      using errcode = '23514';
  end if;

  -- No unreconciled ambiguous usage anywhere in ledger or siblings.
  if exists(select 1 from public.visual_group_usage_ledger l
      where l.gym_id = v_tenant_text and l.ambiguous
        and not public.visual_group_group_reconciled(l.gym_id, l.group_key))
     or exists(select 1 from public.visual_group_usage_sibling s
      where s.gym_id = v_tenant_text and s.ambiguous
        and not public.visual_group_sibling_reconciled(s)) then
    raise exception 'activation refused: unresolved ambiguous usage requires evidence-based reconciliation'
      using errcode = '23514';
  end if;

  -- No cross-date occupied scene: within each linked scene component, every
  -- live reservation must share ONE date (a NULL reserved_date is a distinct
  -- unknown date and conflicts with any dated reservation).
  if exists(select 1 from (
    select (select array_agg(m order by m)
            from public.visual_group_scene_members(l.gym_id, l.group_key) mm(m)) as component,
           count(distinct l.reserved_date) as date_count,
           bool_or(l.reserved_date is null) as has_unknown_date
    from public.visual_group_usage_ledger l
    where l.gym_id = v_tenant_text
    group by 1) c
    where c.date_count > 1 or (c.has_unknown_date and c.date_count>0)) then
    raise exception 'activation refused: cross-date occupied visual scene; reconcile history first'
      using errcode = '23514';
  end if;

  -- Import EVERY tenant's calendar and orphan-ledger history under the same
  -- table-wide barrier. The owner-only importer refuses incomplete coverage
  -- anywhere and imports published history before reservations.
  perform public.visual_global_import_history();

  -- Re-read both calendar and orphan-ledger coverage after import. Any
  -- missing fingerprint, owner/date conflict or member mismatch aborts the
  -- transaction before its activation receipt is written.
  if exists(select 1 from public.visual_global_coverage() c
      where c.issue<>'ready')
     or exists(select 1 from public.visual_global_history_coverage() h
      where h.issue<>'ready') then
    raise exception 'activation refused: global visual history coverage incomplete after import'
      using errcode='23514';
  end if;

  -- 4. Proof + arming in the SAME transaction. The receipt (current txid)
  -- authorizes the settings write through the arm guard; any earlier failure
  -- rolled back every alias, ledger, sibling, event and receipt write.
  select count(*) into v_calendar_rows from public.content_calendar r where r.gym_id = any(v_keys);
  select count(*) into v_ledger_rows from public.visual_group_usage_ledger l where l.gym_id = v_tenant_text;
  select count(*) into v_sibling_rows from public.visual_group_usage_sibling s where s.gym_id = v_tenant_text;
  select count(*) into v_global_rows from public.visual_global_usage_member m where m.tenant_id = v_tenant_text;

  v_proof := jsonb_build_object(
    'tenant', v_tenant_text,
    'actor', v_actor,
    'enforced', true,
    'idempotent', false,
    'covered_keys', to_jsonb(v_keys),
    'calendar_rows', v_calendar_rows,
    'ledger_rows', v_ledger_rows,
    'sibling_rows', v_sibling_rows,
    'global_member_rows', v_global_rows,
    'global_history_imported', true,
    'backfills', v_backfills,
    'barrier', 'lock table content_calendar share row exclusive; try advisory visual_tenant + sorted visual_backfill keys',
    'armed_at', now());

  insert into public.visual_group_activation(gym_id, proof, actor)
    values (v_tenant_text, v_proof, v_actor)
    on conflict (gym_id) do update set proof = excluded.proof, actor = excluded.actor,
      transaction_id = txid_current(), created_at = now();

  insert into public.gym_visual_guard_settings(gym_id, enforce, updated_at)
    values (v_tenant_text, true, now())
    on conflict (gym_id) do update set enforce = true, updated_at = now();

  return v_proof;
end;
$$;

revoke all on function public.visual_group_tenant_alias_arm_guard() from public,anon,authenticated;
revoke all on function public.visual_group_activate_guard(text,text) from public,anon,authenticated;
grant execute on function public.visual_group_activate_guard(text,text) to service_role;
