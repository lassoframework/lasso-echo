-- DRAFT / UNAPPLIED / OFF. Additive fleet ledger coverage gate for historical
-- scene backfill and activation. Apply strictly after DRAFT_visual_scene_history_backfill_20261004.sql
-- (whose whole stack and lock order it preserves). This file grants no production application or
-- activation approval.
--
-- Gap closed: the prior backfill iterated only surviving published
-- content_calendar rows. Permanent exact-byte ledger uses whose calendar rows
-- were later deleted, denied or swapped were never audited, so activation
-- could arm while visual_group_usage_ledger (reserved/published/released),
-- visual_global_usage_member (all states), visual_global_usage owners
-- (including orphan owners with no member) and visual_global_release_history
-- still held unproved obligations.
--
-- This draft ADDITIVELY replaces visual_scene_history_backfill_locked and the
-- activation receipt trigger function (same signatures, same lock order) so
-- coverage = calendar rows + every ledger obligation, each enumerated
-- independently with UNION ALL. No join may erase an obligation: every ledger
-- row is emitted and evaluated on its own; a NULL date is unresolved
-- (missing_date) and never inferred from timestamps, siblings or receipts.
--
-- Proof standard for a ledger obligation whose calendar row is gone or never
-- existed: retained historical identity tied to ACTUAL scene occupancy — a
-- visual_scene_phash_occupied row (written only inside a clean claim
-- transaction) for the same tenant and date whose group is in the
-- obligation's linked scene and whose fingerprint EXACTLY equals the
-- obligation's fingerprint. A same group/date coincidence alone is not proof;
-- a staged candidate is never proof; the current replacement media is never
-- proof; an unproven source/render equivalence is never proof. For
-- obligations that carry no fingerprint (per-gym usage ledger), NOTHING in
-- this schema proves WHICH bytes were used: a calendar_row_id, a surviving
-- published row, a same group/date occupancy row, and an attested
-- replacement member can ALL describe the NEW replacement media while the
-- ledger entry recorded the OLD media — the same calendar row id can carry
-- both durable occupancy records. A matching row id is not original-byte
-- evidence. Lacking a truly immutable, row-specific, byte-bound original-use
-- receipt, such obligations stay UNRESOLVED with reason
-- historical_fingerprint_unproven and require operator review/reconciliation;
-- pHashes and dates are never invented to clear them. Unprovable records are marked
-- unresolved — pHashes and dates are never invented, and ledger obligations
-- NEVER write visual_scene_phash_occupied.
--
-- The activation receipt trigger now ALWAYS recomputes authoritative fleet
-- coverage inside the activation transaction via
-- visual_scene_history_backfill_locked(null) and overwrites any incoming
-- proof->scene_history receipt. A pre-computed or stale receipt (including
-- one written by the previous draft) can never authorize activation.
--
-- Preserved lock order:
--   1. content_calendar SHARE ROW EXCLUSIVE write barrier;
--   2. calendar rows FOR UPDATE plus a proof-only plan; component targets
--      collected from the calendar AND all ledgers, locked sorted NOWAIT;
--   3. NOWAIT proof/ledger relation barriers (including the four ledger
--      relations);
--   4. the one fleet-wide visual_scene_global advisory lock, LAST;
--   5. under the full lock set: ledger obligations are re-read, drift is
--      detected (never waited on after the global lock) and is fail-closed —
--      any drift skips every write; each obligation and each live calendar
--      row is then re-evaluated.
--
-- The diagnostic audit surfaces remain read-only and service_role-only; every
-- mutation helper stays owner-only.

begin;

-- Refuse an unsafe late install: NO tenant may already be armed, period.
-- A stored JSON coverage label on an activation receipt is not trusted: a
-- receipt claiming ledger-aware coverage could have been forged (or written
-- verbatim) under the previous hook, so its presence never waives refusal.
-- Install this draft only over a fully unarmed fleet.
do $$
begin
  if exists(select 1 from public.gym_visual_guard_settings s
      where s.enforce) then
    raise exception 'scene ledger coverage gate must be installed before any tenant is armed'
      using errcode='55000';
  end if;
end;
$$;

-- Independent enumeration of EVERY ledger obligation. UNION ALL only: no join
-- may collapse or erase a row. Unmapped tenants stay visible (tenant_id NULL)
-- so a fleet run still audits them; a tenant-scoped run never claims them.
create or replace function public.visual_scene_ledger_obligations(
  p_tenant text default null
) returns table(
  kind text, raw_gym_key text, tenant_id text, group_key text,
  fingerprint text, used_date date, calendar_row_id uuid, channel text,
  state text
) language sql stable security definer set search_path = public as $$
  select o.kind, o.raw_gym_key, o.tenant_id, o.group_key, o.fingerprint,
    o.used_date, o.calendar_row_id, o.channel, o.state
  from (
    select 'group_usage_ledger'::text as kind, l.gym_id as raw_gym_key,
      public.visual_group_tenant_id(l.gym_id)::text as tenant_id,
      l.group_key, null::text as fingerprint, l.reserved_date as used_date,
      l.calendar_row_id, l.channel, l.state
      from public.visual_group_usage_ledger l
    union all
    select 'global_usage_member', m.tenant_id, m.tenant_id, m.group_key,
      m.fingerprint, m.used_date, m.calendar_row_id, m.channel, m.state
      from public.visual_global_usage_member m
    union all
    -- Orphan owners included: an owner with no member is still an obligation.
    select 'global_usage_owner', u.tenant_id, u.tenant_id, null::text,
      u.fingerprint, u.used_date, null::uuid, null::text, u.state
      from public.visual_global_usage u
    union all
    select 'release_history', r.tenant_id, r.tenant_id, r.group_key,
      r.fingerprint, r.used_date, r.calendar_row_id, null::text, 'released'
      from public.visual_global_release_history r
  ) o
  where p_tenant is null or o.tenant_id = p_tenant;
$$;

-- Proof evaluation of ONE ledger obligation. Writes and locks nothing.
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
  v_fp text := p_obligation->>'fingerprint';
  v_grp text := p_obligation->>'group_key';
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

  -- FAIL-CLOSED: a fingerprint-free obligation (per-gym usage ledger) can
  -- never prove WHICH bytes it used. Neither a surviving published calendar
  -- row (Path 1) nor a matching occupied row (Path 2) is original-byte
  -- evidence: the SAME calendar row id can hold old media A's ledger entry
  -- and replacement media B's occupancy/published row. Unless a truly
  -- immutable, row-specific, byte-bound original-use receipt exists (none in
  -- this schema), the obligation stays UNRESOLVED for operator review and
  -- reconciliation. Dates and pHashes are never invented to clear it.
  if v_fp is null then
    o_reason := 'historical_fingerprint_unproven'; return;
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

-- Read-only operator surface for ledger coverage. Never locks or mutates.
create or replace function public.visual_scene_ledger_coverage_audit(
  p_tenant text default null
) returns table(
  kind text, raw_gym_key text, tenant_id text, group_key text,
  fingerprint text, used_date date, calendar_row_id uuid, channel text,
  state text, audit_status text, reason text,
  occupied_phash char(16), occupied_fingerprint text
) language plpgsql stable security definer set search_path = public as $$
declare v_ob record; v_eval record;
begin
  for v_ob in
    select o.* from public.visual_scene_ledger_obligations(p_tenant) o
    order by o.kind, o.raw_gym_key, o.tenant_id, o.group_key,
      o.fingerprint, o.used_date, o.calendar_row_id
  loop
    select * into v_eval
      from public.visual_scene_ledger_obligation_evaluate(to_jsonb(v_ob));
    kind := v_ob.kind; raw_gym_key := v_ob.raw_gym_key;
    tenant_id := v_ob.tenant_id; group_key := v_ob.group_key;
    fingerprint := v_ob.fingerprint; used_date := v_ob.used_date;
    calendar_row_id := v_ob.calendar_row_id; channel := v_ob.channel;
    state := v_ob.state;
    audit_status := v_eval.o_status; reason := v_eval.o_reason;
    occupied_phash := v_eval.o_phash;
    occupied_fingerprint := v_eval.o_occupied_fingerprint;
    return next;
  end loop;
end;
$$;

-- Additive replacement of the locked writer: same signature, same lock order,
-- calendar coverage PLUS every ledger obligation. Detailed results persist in
-- activation proof; the integer wrapper below keeps its return type.
create or replace function public.visual_scene_history_backfill_locked(
  p_tenant text default null
) returns jsonb language plpgsql security definer set search_path = public as $$
declare
  v_row public.content_calendar%rowtype;
  v_live public.content_calendar%rowtype;
  v_eval record;
  v_leval record;
  v_plan jsonb;
  v_plans jsonb := '[]'::jsonb;
  v_components jsonb := '[]'::jsonb;
  v_results jsonb := '[]'::jsonb;
  v_conflicts jsonb;
  v_obligation jsonb;
  v_obligations_plan jsonb;
  v_obligations_live jsonb;
  v_ledger_drift boolean := false;
  v_ledger_checked integer := 0;
  v_ledger_unresolved integer := 0;
  v_occupied_fp text;
  v_inserted integer := 0;
  v_already integer := 0;
  v_unresolved integer := 0;
  v_row_count integer;
begin
  -- Same table-first barrier as activation. This drains calendar writers
  -- before any proof read and makes the later receipt cover a stable set.
  lock table public.content_calendar in share row exclusive mode;
  -- Lock and snapshot every target row before scene locks. Evaluation is only
  -- a plan: proof tables remain mutable until their relation barriers are taken
  -- after the components, then every row and proof field is re-evaluated.
  for v_row in
    select c.* from public.content_calendar c
    where (c.status='published' or c.published_at is not null)
      and (p_tenant is null
        or public.visual_group_tenant_id(c.gym_id)::text=p_tenant)
    order by c.id for update
  loop
    select * into v_eval from public.visual_scene_history_evaluate(v_row);
    v_plans := v_plans || jsonb_build_array(jsonb_build_object(
      'row',to_jsonb(v_row),'evaluation',to_jsonb(v_eval)));
    if v_eval.o_status='ready' then
      v_components := v_components || jsonb_build_array(jsonb_build_object(
        'gym_id',v_eval.o_tenant,'group_key',v_eval.o_group_key));
    end if;
  end loop;

  -- Snapshot every ledger obligation (all four ledgers, independently) and
  -- add their (gym, group) pairs to the sorted component lock targets. A
  -- ledger-only group whose calendar rows are gone is still locked.
  select coalesce(jsonb_agg(to_jsonb(o)
      order by o.kind,o.raw_gym_key,o.tenant_id,o.group_key,o.fingerprint,
        o.used_date,o.calendar_row_id),'[]'::jsonb)
    into v_obligations_plan
    from public.visual_scene_ledger_obligations(p_tenant) o;
  select v_components || coalesce(jsonb_agg(jsonb_build_object(
      'gym_id',o.raw_gym_key,'group_key',o.group_key)),'[]'::jsonb)
    into v_components
    from public.visual_scene_ledger_obligations(p_tenant) o
    where o.raw_gym_key is not null and o.group_key is not null;

  -- Required common order with exact-object preparation: component first,
  -- proof relations second. Do not wait for an unrelated proof writer while
  -- holding components; its fresh transaction can retry after SQLSTATE 55P03.
  perform public.visual_scene_history_lock_components_nowait(v_components);
  begin
    lock table public.visual_scene_candidate,
      public.visual_scene_phash_occupied,
      public.visual_group_alias, public.visual_group_scene_link,
      public.visual_global_object_attestation,
      public.visual_global_scene_object_member,
      public.visual_global_object_lineage,
      public.visual_group_usage_ledger,
      public.visual_global_usage,
      public.visual_global_usage_member,
      public.visual_global_release_history
      in share row exclusive mode nowait;
  exception when lock_not_available then
    raise exception 'scene proof writer busy; retry transaction'
      using errcode='55P03';
  end;
  -- The one fleet-wide advisory lock is taken LAST. Nothing waits after it.
  if not pg_try_advisory_xact_lock(hashtextextended(
      jsonb_build_array('visual_scene_global')::text,0)) then
    raise exception 'global scene claim busy; retry transaction'
      using errcode='55P03';
  end if;

  -- Re-read every ledger obligation under the complete lock set. Drift is
  -- detected, never waited on: any changed/added/removed obligation is
  -- fail-closed unresolved coverage and skips EVERY write below.
  select coalesce(jsonb_agg(to_jsonb(o)
      order by o.kind,o.raw_gym_key,o.tenant_id,o.group_key,o.fingerprint,
        o.used_date,o.calendar_row_id),'[]'::jsonb)
    into v_obligations_live
    from public.visual_scene_ledger_obligations(p_tenant) o;
  if v_obligations_live is distinct from v_obligations_plan then
    v_ledger_drift := true;
    v_ledger_unresolved := v_ledger_unresolved + 1;
    v_results := v_results || jsonb_build_array(jsonb_build_object(
      'kind','ledger_drift','status','unresolved',
      'reason','ledger_drift_under_lock'));
  end if;

  if not v_ledger_drift then
    for v_plan in select value from jsonb_array_elements(v_plans)
    loop
      -- Reload the LIVE row after the complete lock set. Never re-evaluate
      -- the stale loop record used to plan component locks.
      select c.* into v_live from public.content_calendar c
        where c.id=(v_plan->'evaluation'->>'o_calendar_row_id')::uuid
        for update;
      if not found then
        v_unresolved := v_unresolved + 1;
        v_results := v_results || jsonb_build_array(jsonb_build_object(
          'calendar_row_id',v_plan->'evaluation'->>'o_calendar_row_id',
          'status','unresolved','reason','row_missing_under_lock'));
        continue;
      end if;
      select * into v_eval from public.visual_scene_history_evaluate(v_live);

      -- Full proof-field equality: the entire live calendar composite and
      -- every evaluator output (tenant/group/date/candidate/pHash/
      -- fingerprints/URLs/roles/source/channel/publication markers) must
      -- match the locked plan.
      if to_jsonb(v_live) is distinct from v_plan->'row'
          or to_jsonb(v_eval) is distinct from v_plan->'evaluation' then
        v_unresolved := v_unresolved + 1;
        v_results := v_results || jsonb_build_array(jsonb_build_object(
          'calendar_row_id',v_live.id,'status','unresolved',
          'reason','proof_drift_under_lock'));
        continue;
      end if;

      if v_eval.o_status <> 'ready' then
        v_unresolved := v_unresolved + 1;
        v_results := v_results || jsonb_build_array(jsonb_build_object(
          'calendar_row_id',v_live.id,'status','unresolved',
          'reason',v_eval.o_reason,
          'raw_gym_key',v_eval.o_raw_gym_key,
          'persisted_group_key',v_eval.o_persisted_group_key));
        continue;
      end if;

      -- Exact key match alone is not identity: the stored fingerprint must
      -- equal the live row's exact bytes. Same pHash/tenant/group/date over
      -- different bytes (old vs new media) is unresolved, never recorded.
      select o.fingerprint into v_occupied_fp
        from public.visual_scene_phash_occupied o
        where o.phash=v_eval.o_phash and o.tenant_id=v_eval.o_tenant
          and o.group_key=v_eval.o_group_key
          and o.used_date=v_eval.o_post_date;
      if found and v_occupied_fp is distinct from v_eval.o_fingerprint then
        v_unresolved := v_unresolved + 1;
        v_results := v_results || jsonb_build_array(jsonb_build_object(
          'calendar_row_id',v_live.id,'status','unresolved',
          'reason','occupancy_fingerprint_mismatch',
          'tenant_id',v_eval.o_tenant,'group_key',v_eval.o_group_key,
          'post_date',v_eval.o_post_date,'phash',v_eval.o_phash,
          'fingerprint',v_eval.o_fingerprint,
          'occupied_fingerprint',v_occupied_fp));
        continue;
      end if;
      if found then
        v_already := v_already + 1;
        v_results := v_results || jsonb_build_array(jsonb_build_object(
          'calendar_row_id',v_live.id,'status','already_recorded',
          'tenant_id',v_eval.o_tenant,'group_key',v_eval.o_group_key,
          'post_date',v_eval.o_post_date,'candidate_id',v_eval.o_candidate_id,
          'phash',v_eval.o_phash,'fingerprint',v_eval.o_fingerprint,
          'exact_url',v_eval.o_exact_url,'object_role',v_eval.o_object_role,
          'source_url',v_eval.o_source_url,
          'source_fingerprint',v_eval.o_source_fingerprint));
        continue;
      end if;

      select jsonb_agg(jsonb_build_object(
        'phash',o.phash,'tenant_id',o.tenant_id,'group_key',o.group_key,
        'used_date',o.used_date,
        'hamming',public.visual_scene_hamming(v_eval.o_phash,o.phash))
        order by public.visual_scene_hamming(v_eval.o_phash,o.phash),
          o.phash,o.tenant_id,o.group_key,o.used_date)
      into v_conflicts
      from public.visual_scene_phash_occupied o
      where public.visual_scene_hamming(v_eval.o_phash,o.phash) between 0 and 30
        and not (o.tenant_id=v_eval.o_tenant
          and o.used_date=v_eval.o_post_date
          and o.group_key in (select sm.group_key from
            public.visual_group_scene_members(v_eval.o_tenant,
              v_eval.o_group_key) sm(group_key)));
      if v_conflicts is not null then
        v_unresolved := v_unresolved + 1;
        v_results := v_results || jsonb_build_array(jsonb_build_object(
          'calendar_row_id',v_live.id,'status','unresolved',
          'reason','historical_scene_conflict','conflicts',v_conflicts));
        continue;
      end if;

      insert into public.visual_scene_phash_occupied
        (phash,tenant_id,group_key,used_date,fingerprint,
         calendar_row_id,channel,evidence)
      values (v_eval.o_phash,v_eval.o_tenant,v_eval.o_group_key,
        v_eval.o_post_date,v_eval.o_fingerprint,v_live.id,v_eval.o_channel,
        jsonb_build_object(
          'proof','backfill_exact_byte_ledger',
          'calendar_row_id',v_live.id,
          'raw_gym_key',v_eval.o_raw_gym_key,
          'tenant_id',v_eval.o_tenant,
          'group_key',v_eval.o_group_key,
          'post_date',v_eval.o_post_date,
          'channel',v_eval.o_channel,
          'published_at',v_eval.o_published_at,
          'candidate_id',v_eval.o_candidate_id,
          'phash',v_eval.o_phash,
          'fingerprint',v_eval.o_fingerprint,
          'exact_url',v_eval.o_exact_url,
          'object_role',v_eval.o_object_role,
          'source_url',v_eval.o_source_url,
          'source_fingerprint',v_eval.o_source_fingerprint))
      on conflict (phash,tenant_id,group_key,used_date) do nothing;
      get diagnostics v_row_count = row_count;
      if v_row_count=1 then
        v_inserted := v_inserted + 1;
        v_results := v_results || jsonb_build_array(jsonb_build_object(
          'calendar_row_id',v_live.id,'status','recorded',
          'tenant_id',v_eval.o_tenant,'group_key',v_eval.o_group_key,
          'post_date',v_eval.o_post_date,'candidate_id',v_eval.o_candidate_id,
          'phash',v_eval.o_phash,'fingerprint',v_eval.o_fingerprint,
          'exact_url',v_eval.o_exact_url,'object_role',v_eval.o_object_role,
          'source_url',v_eval.o_source_url,
          'source_fingerprint',v_eval.o_source_fingerprint));
      else
        v_already := v_already + 1;
      end if;
    end loop;

    -- Ledger obligations are PROOF CHECKS ONLY: they never write
    -- visual_scene_phash_occupied and never invent pHashes or dates.
    for v_obligation in
      select value from jsonb_array_elements(v_obligations_live)
    loop
      select * into v_leval
        from public.visual_scene_ledger_obligation_evaluate(v_obligation);
      v_ledger_checked := v_ledger_checked + 1;
      if v_leval.o_status <> 'resolved' then
        v_ledger_unresolved := v_ledger_unresolved + 1;
      end if;
      v_results := v_results || jsonb_build_array(jsonb_build_object(
        'kind',v_obligation->>'kind',
        'status',v_leval.o_status,
        'reason',v_leval.o_reason,
        'raw_gym_key',v_obligation->>'raw_gym_key',
        'tenant_id',v_obligation->>'tenant_id',
        'group_key',v_obligation->>'group_key',
        'fingerprint',v_obligation->>'fingerprint',
        'used_date',v_obligation->>'used_date',
        'calendar_row_id',v_obligation->>'calendar_row_id',
        'channel',v_obligation->>'channel',
        'state',v_obligation->>'state',
        'occupied_phash',v_leval.o_phash,
        'occupied_fingerprint',v_leval.o_occupied_fingerprint));
    end loop;
  end if;

  return jsonb_build_object(
    'scope',case when p_tenant is null then 'fleet' else 'tenant' end,
    'coverage','calendar_and_ledger',
    'tenant_id',p_tenant,
    'transaction_id',txid_current(),
    'covered_published_rows',jsonb_array_length(v_plans),
    'ledger_obligations',v_ledger_checked,
    'ledger_unresolved',v_ledger_unresolved,
    'ledger_drift',v_ledger_drift,
    'barrier','content_calendar share row exclusive',
    'lock_order','calendar barrier and rows; all scene components nowait from calendar+ledgers; proof and ledger relation barriers nowait; fleet scene lock',
    'inserted',v_inserted,'already_recorded',v_already,
    'unresolved',v_unresolved + v_ledger_unresolved,'rows',v_results);
end;
$$;

-- Keep the frozen wave's owner-only compatibility wrapper on its return type.
create or replace function public.visual_scene_backfill_occupied()
returns integer language plpgsql security definer set search_path = public as $$
declare v_receipt jsonb;
begin
  v_receipt := public.visual_scene_history_backfill_locked(null);
  return (v_receipt->>'inserted')::integer;
end;
$$;

-- Same-transaction activation hook. It runs only while the activation RPC's
-- calendar write barrier is held and ALWAYS recomputes authoritative fleet
-- coverage inside this transaction. An incoming proof->scene_history receipt
-- is never trusted: it is overwritten by the recomputed one, so a stale or
-- pre-ledger receipt can never authorize activation. Any unresolved calendar
-- row or ledger obligation refuses activation fleet-wide and rolls the whole
-- arming transaction back (no activation row, no settings row, no occupied
-- writes survive).
create or replace function public.visual_scene_history_activation_receipt()
returns trigger language plpgsql security definer set search_path = public as $$
declare v_receipt jsonb;
begin
  if not exists(select 1 from pg_locks l
      where l.pid=pg_backend_pid() and l.locktype='relation'
        and l.relation='public.content_calendar'::regclass
        and l.mode='ShareRowExclusiveLock' and l.granted) then
    raise exception 'scene history activation receipt requires the calendar write barrier'
      using errcode='25006';
  end if;
  -- Recompute authoritative coverage; never trust the incoming receipt.
  v_receipt := public.visual_scene_history_backfill_locked(null);
  if coalesce(v_receipt->>'scope','') <> 'fleet'
      or coalesce(v_receipt->>'coverage','') <> 'calendar_and_ledger'
      or coalesce((v_receipt->>'unresolved')::integer,-1) <> 0 then
    raise exception 'activation refused: unresolved scene ledger coverage requires review'
      using errcode='23514', detail=v_receipt::text;
  end if;
  new.proof := coalesce(new.proof,'{}'::jsonb) ||
    jsonb_build_object('scene_history',v_receipt);
  return new;
end;
$$;

-- Mutation helpers stay owner-only; the diagnostic audits stay read-only and
-- service_role-only.
revoke all on function public.visual_scene_ledger_obligations(text)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_scene_ledger_obligation_evaluate(jsonb)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_scene_history_backfill_locked(text)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_scene_backfill_occupied()
  from public,anon,authenticated,service_role;
revoke all on function public.visual_scene_history_activation_receipt()
  from public,anon,authenticated,service_role;
revoke all on function public.visual_scene_ledger_coverage_audit(text)
  from public,anon,authenticated;
grant execute on function public.visual_scene_ledger_coverage_audit(text)
  to service_role;
grant execute on function public.visual_scene_history_audit(text)
  to service_role;

commit;
