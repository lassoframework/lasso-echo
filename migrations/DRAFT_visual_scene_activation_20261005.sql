-- DRAFT / UNAPPLIED / OFF — scene-specific activation, never apply to production.
-- Apply only after DRAFT_visual_group_activation_20261002.sql,
-- DRAFT_visual_scene_ledger_20261005.sql,
-- DRAFT_visual_scene_history_backfill_20261005.sql, and
-- DRAFT_visual_scene_calendar_transaction_20261005.sql in a disposable review.
-- This is the LAST migration in the draft chain. It replaces only the
-- service-role activation RPC. All settings defaults remain OFF; loading this
-- file does not arm any tenant or write scene occupancy.
--
-- Runtime contract: use one fresh READ COMMITTED transaction. The canonical
-- calendar writer barrier is acquired first with a five-second lock timeout;
-- auxiliary table locks are NOWAIT and advisory locks are TRY. Scene history
-- backfill, complete fleet-wide coverage, conflict/hold refusal, the receipt
-- and one tenant's enforce=true write all happen in that one transaction.
-- Any error rolls the transaction back, including occupied backfill rows.
-- Rollback before activation: restore visual_group_activate_guard from the
-- group activation draft. After live activation, preserve all append-only
-- occupied history and use an owner-reviewed disarm/restore plan.
begin;

do $$ begin
  if to_regprocedure('public.visual_scene_history_coverage()') is null
     or to_regprocedure('public.visual_scene_backfill_occupied()') is null
     or to_regprocedure('public.visual_scene_hamming(text,text)') is null
     or to_regclass('public.visual_scene_phash_occupied') is null
     or to_regclass('public.visual_scene_review_hold') is null
     or to_regclass('public.visual_scene_owner_phash_receipt') is null
     or to_regclass('public.visual_scene_candidate') is null then
    raise exception 'scene ledger and historical occupied backfill must precede activation'
      using errcode='55000';
  end if;
  if not exists(select 1 from pg_trigger t
      join pg_proc f on f.oid=t.tgfoid
      where t.tgrelid='public.content_calendar'::regclass
        and t.tgname='content_calendar_visual_group_guard'
        and not t.tgisinternal and t.tgenabled<>'D'
        and f.prosrc like '%visual_scene_claim_scan%'
        and f.prosrc like '%visual_scene_phash_occupied%')
     or not exists(select 1 from pg_proc f
       where f.oid=to_regprocedure('public.claim_calendar_publish_slot_owned(uuid,text,date,text,integer,boolean)')
         and f.prosrc like '%v_row.publish_claim_token is distinct from v_token%')
     or not exists(select 1 from pg_proc f
       where f.oid=to_regprocedure('public.approve_calendar_row_if_media_ready(uuid,text)')
         and f.prosrc like '%variant_status=''active''%') then
    raise exception 'scene calendar claim, publish and approval wiring must precede activation'
      using errcode='55000';
  end if;
end $$;

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
  v_scene_occupied_rows integer;
  v_scene_backfilled integer := 0;
  v_previous_lock_timeout text;
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

  -- Bound waits for the calendar writer barrier. Advisory and auxiliary
  -- table locks below use TRY/NOWAIT. Restore the prior timeout on success.
  v_previous_lock_timeout := current_setting('lock_timeout');
  perform set_config('lock_timeout', '5s', true);

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
    public.visual_global_usage, public.visual_global_usage_member,
    public.gym_visual_guard_settings,
    public.visual_scene_owner_phash_receipt,
    public.visual_scene_candidate, public.visual_scene_phash_occupied,
    public.visual_scene_review_hold
    in share row exclusive mode nowait;

  -- 2. The SAME canonical mutex every auxiliary mutation RPC takes before
  -- its other locks. Never wait after the barrier: an auxiliary writer may
  -- own this lock while queued for calendar DML that our barrier blocks.
  if not pg_try_advisory_xact_lock(hashtextextended(
    jsonb_build_array('visual_tenant',v_tenant_text)::text,0)) then
    raise exception 'activation refused: canonical tenant auxiliary writer busy; retry transaction'
      using errcode='55P03';
  end if;

  -- Scene claim writers take this fleet-wide advisory after their calendar
  -- row/component locks. Never wait while holding the calendar barrier.
  if not pg_try_advisory_xact_lock(hashtextextended(
      jsonb_build_array('visual_scene_global')::text, 0)) then
    raise exception 'activation refused: scene claim or backfill busy; retry transaction'
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

  -- Scene blockers are explicit even if the byte-history import would also
  -- fail for the same published row. No scene occupancy is written yet.
  if exists(select 1 from public.visual_scene_history_coverage()
      where state = 'blocked') then
    raise exception 'activation refused: historical scene coverage has blocked rows'
      using errcode='23514';
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

  -- Scene history is fleet-wide: every published calendar row, including
  -- other tenants, must be evidenced before any tenant can arm. The backfill
  -- writer is intentionally pre-activation only. Once one tenant is armed,
  -- later activations can proceed only if all history remains covered; new
  -- backfillable history requires disarming first and a fresh full import.
  if not exists(select 1 from public.gym_visual_guard_settings where enforce) then
    v_scene_backfilled := public.visual_scene_backfill_occupied();
  end if;
  if exists(select 1 from public.visual_scene_history_coverage()
      where state <> 'covered') then
    raise exception 'activation refused: historical scenes remain unoccupied or blocked after backfill'
      using errcode='23514';
  end if;
  -- A coverage report row is required for every published row. The report
  -- loops all published rows, but count parity catches a future report edit
  -- that accidentally filters a class of historical calendar rows.
  if (select count(*) from public.visual_scene_history_coverage()) <>
     (select count(*) from public.content_calendar c
      where c.status = 'published' or c.published_at is not null) then
    raise exception 'activation refused: historical scene coverage row count mismatch'
      using errcode='23514';
  end if;
  -- The claim policy treats <=30 Hamming distance across distinct usage
  -- scopes as a hold. Existing occupied history needs the same ruling before
  -- enabling the guard; an approved claim hold is not blanket historical
  -- clearance. Identical tenant/group/date siblings share one scope.
  if exists(select 1 from public.visual_scene_phash_occupied a
      join public.visual_scene_phash_occupied b
        on (a.phash,a.tenant_id,a.group_key,a.used_date) <
           (b.phash,b.tenant_id,b.group_key,b.used_date)
      where (a.tenant_id,a.group_key,a.used_date) <>
            (b.tenant_id,b.group_key,b.used_date)
        and public.visual_scene_hamming(a.phash,b.phash) <= 30) then
    raise exception 'activation refused: historical occupied scenes have unresolved near-frame or uncertain similarity conflicts'
      using errcode='23514';
  end if;
  if exists(select 1 from public.visual_scene_review_hold h
      where h.state = 'open') then
    raise exception 'activation refused: open scene review holds remain'
      using errcode='23514';
  end if;

  -- 4. Proof + arming in the SAME transaction. The receipt (current txid)
  -- authorizes the settings write through the arm guard; any earlier failure
  -- rolled back every alias, ledger, sibling, event and receipt write.
  select count(*) into v_calendar_rows from public.content_calendar r where r.gym_id = any(v_keys);
  select count(*) into v_ledger_rows from public.visual_group_usage_ledger l where l.gym_id = v_tenant_text;
  select count(*) into v_sibling_rows from public.visual_group_usage_sibling s where s.gym_id = v_tenant_text;
  select count(*) into v_global_rows from public.visual_global_usage_member m where m.tenant_id = v_tenant_text;
  select count(*) into v_scene_occupied_rows from public.visual_scene_phash_occupied;

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
    'scene_history_covered', true,
    'scene_rows_backfilled_this_transaction', v_scene_backfilled,
    'scene_occupied_rows', v_scene_occupied_rows,
    'backfills', v_backfills,
    'barrier', 'bounded calendar writer lock; NOWAIT auxiliary tables; TRY tenant, scene and sorted backfill advisories',
    'armed_at', now());

  insert into public.visual_group_activation(gym_id, proof, actor)
    values (v_tenant_text, v_proof, v_actor)
    on conflict (gym_id) do update set proof = excluded.proof, actor = excluded.actor,
      transaction_id = txid_current(), created_at = now();

  insert into public.gym_visual_guard_settings(gym_id, enforce, updated_at)
    values (v_tenant_text, true, now())
    on conflict (gym_id) do update set enforce = true, updated_at = now();

  perform set_config('lock_timeout', v_previous_lock_timeout, true);
  return v_proof;
end;
$$;

-- Preserve the service-only API. Owner-only backfill remains ungranted.
revoke all on function public.visual_group_activate_guard(text,text)
  from public, anon, authenticated, service_role;
grant execute on function public.visual_group_activate_guard(text,text) to service_role;
commit;
