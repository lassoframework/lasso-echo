-- DRAFT / UNAPPLIED / OFF. Additive historical scene backfill and activation
-- gate. Apply strictly after DRAFT_visual_scene_claim_wave_20261003.sql and
-- DRAFT_visual_group_activation_20261002.sql. This file grants no production
-- application or activation approval.
--
-- The 2026-10-03 scene claim wave is frozen. Its fail-closed 0A000 stub stays
-- byte-identical in source; this later draft replaces that stub and adds the
-- activation receipt hook. Unknown, source-null, ambiguous, stale or otherwise
-- unproved published history is reported and blocks activation. It is never
-- inferred and never written to visual_scene_phash_occupied.
--
-- Lock order for a backfill transaction:
--   1. content_calendar SHARE ROW EXCLUSIVE write barrier;
--   2. live calendar rows FOR UPDATE and a proof-only component plan;
--   3. all resolved scene components in deterministic order, NOWAIT;
--   4. NOWAIT proof/scene relation barriers;
--   5. the one fleet-wide visual_scene_global transaction advisory lock.
-- Exact-object preparation also takes the component before writing its proof
-- relations. Keeping the same order prevents prepare-versus-backfill lock
-- inversion; NOWAIT makes an unrelated proof writer an explicit retry.
-- The activation receipt trigger runs inside the existing activation RPC after
-- that RPC acquired its calendar barrier and completed its other coverage
-- checks. It always covers the entire fleet, even when one tenant is being
-- armed. A clean scene-history receipt is added to the same persisted
-- visual_group_activation.proof that authorizes enforce=true. The activation
-- RPC returns its pre-trigger proof; callers must read visual_group_activation
-- after success for the authoritative scene_history receipt.
--
-- A source-null published row is itself the durable review evidence: this
-- draft never mutates or infers its missing identity. The read-only
-- visual_scene_history_audit view continues to report reason=source_null and
-- every activation remains off until the row is repaired. Because a refused
-- activation rolls back atomically, it intentionally does not write a separate
-- review event or a receipt that could be mistaken for successful coverage.

begin;

-- Refuse an unsafe late install. This draft must precede every first arm so an
-- older receipt can never authorize a tenant without scene-history coverage.
do $$
begin
  if exists(select 1 from public.gym_visual_guard_settings s
      where s.enforce and not exists(
        select 1 from public.visual_group_activation a
        where a.gym_id=s.gym_id
          and a.proof->'scene_history'->>'scope'='fleet'
          and coalesce((a.proof->'scene_history'->>'unresolved')::integer,-1)=0)) then
    raise exception 'scene history activation gate must be installed before any tenant is armed'
      using errcode='55000';
  end if;
end;
$$;

-- Proof-only row evaluation. The function resolves identity from exact row
-- aliases, binds exactly one candidate to the displayed object, and verifies
-- both the selected source and delivered bytes. It writes and locks nothing.
create or replace function public.visual_scene_history_evaluate(
  p_row public.content_calendar,
  out o_status text, out o_reason text,
  out o_calendar_row_id uuid, out o_raw_gym_key text,
  out o_tenant text, out o_group_key text,
  out o_persisted_group_key text, out o_post_date date,
  out o_channel text, out o_published_at timestamptz,
  out o_candidate_id uuid, out o_phash char(16),
  out o_fingerprint text, out o_exact_url text,
  out o_object_role text, out o_source_url text,
  out o_source_fingerprint text
) language plpgsql stable security definer set search_path = public as $$
declare
  v_proof_row public.content_calendar%rowtype;
  v_delivered record;
  v_candidate_count integer;
begin
  o_status := 'unresolved';
  o_calendar_row_id := p_row.id;
  o_raw_gym_key := p_row.gym_id;
  o_persisted_group_key := p_row.visual_group_key;
  o_post_date := p_row.post_date;
  o_channel := p_row.account;
  o_published_at := p_row.published_at;

  if not (p_row.status = 'published' or p_row.published_at is not null) then
    o_reason := 'not_published'; return;
  end if;
  if p_row.post_date is null then
    o_reason := 'missing_date'; return;
  end if;

  -- No source means no selected source identity. Do not use the delivered
  -- object, candidate, persisted key or a same-URL guess as a substitute.
  o_source_url := nullif(btrim(to_jsonb(p_row)->>'source_media_url'), '');
  if o_source_url is null then
    o_reason := 'source_null'; return;
  end if;

  o_tenant := public.visual_group_tenant_id(p_row.gym_id)::text;
  if o_tenant is null then
    o_reason := 'unmapped_tenant'; return;
  end if;

  begin
    o_group_key := public.visual_group_resolve_row(p_row);
  exception when others then
    o_group_key := null;
    o_reason := 'ambiguous_identity'; return;
  end;
  if o_group_key is null then
    o_reason := 'unresolved_group'; return;
  end if;
  if o_persisted_group_key is not null
      and o_persisted_group_key is distinct from o_group_key then
    o_group_key := null;
    o_reason := 'stale_group_key'; return;
  end if;
  if not exists(select 1 from public.visual_group g
      where g.gym_id=o_tenant and g.group_key=o_group_key) then
    o_group_key := null;
    o_reason := 'unresolved_group'; return;
  end if;

  -- Use the exact re-resolved group only for proof evaluation. The persisted
  -- calendar row is never rewritten by this backfill.
  v_proof_row := p_row;
  v_proof_row.visual_group_key := o_group_key;
  select d.object_role, d.exact_url into v_delivered
    from public.visual_scene_row_delivered_object(v_proof_row) d;
  if v_delivered.exact_url is null then
    o_reason := 'missing_delivered_object'; return;
  end if;

  -- Exactly one candidate row must bind to the displayed object. Multiple
  -- rows remain ambiguous even when their pHashes happen to match.
  select count(*), min(c.candidate_id::text)::uuid
    into v_candidate_count, o_candidate_id
    from public.visual_scene_candidate c
    where c.tenant_id=o_tenant and c.group_key=o_group_key
      and c.object_role=v_delivered.object_role
      and c.exact_url=v_delivered.exact_url;
  if v_candidate_count = 0 then
    o_candidate_id := null;
    o_reason := 'no_bound_candidate'; return;
  elsif v_candidate_count <> 1 then
    o_candidate_id := null;
    o_reason := 'ambiguous_candidate'; return;
  end if;

  select c.phash, c.fingerprint, c.exact_url, c.object_role
    into o_phash, o_fingerprint, o_exact_url, o_object_role
    from public.visual_scene_candidate c
    where c.candidate_id=o_candidate_id;

  if not public.visual_global_row_bytes_verified(v_proof_row) then
    o_reason := 'delivered_bytes_unattested'; return;
  end if;
  select m.fingerprint into o_source_fingerprint
    from public.visual_global_scene_object_member m
    where m.tenant_id=o_tenant and m.exact_url=o_source_url
      and m.object_role='source'
      and m.group_key in (
        select sm.group_key
        from public.visual_group_scene_members(o_tenant,o_group_key) sm(group_key));
  if o_source_fingerprint is null then
    o_reason := 'source_unattested'; return;
  end if;

  o_status := 'ready';
  o_reason := null;
end;
$$;

-- Read-only operator surface. It reports proof readiness plus conflicts with
-- already occupied history, but never locks or mutates. The locked writer below
-- independently reloads and re-evaluates every row before writing.
create or replace function public.visual_scene_history_audit(p_tenant text default null)
returns table(
  calendar_row_id uuid, raw_gym_key text, tenant_id text, group_key text,
  persisted_group_key text, post_date date, channel text,
  published_at timestamptz, backfill_status text, reason text,
  candidate_id uuid, phash char(16), fingerprint text, exact_url text,
  object_role text, source_url text, source_fingerprint text,
  conflicts jsonb
) language plpgsql stable security definer set search_path = public as $$
declare v_row public.content_calendar%rowtype; v_eval record; v_conflicts jsonb;
begin
  for v_row in
    select c.* from public.content_calendar c
    where (c.status='published' or c.published_at is not null)
      and (p_tenant is null
        or public.visual_group_tenant_id(c.gym_id)::text=p_tenant)
    order by c.id
  loop
    select * into v_eval from public.visual_scene_history_evaluate(v_row);
    v_conflicts := null;
    if v_eval.o_status='ready' then
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
      if exists(select 1 from public.visual_scene_phash_occupied o
          where o.phash=v_eval.o_phash and o.tenant_id=v_eval.o_tenant
            and o.group_key=v_eval.o_group_key
            and o.used_date=v_eval.o_post_date) then
        v_eval.o_status := 'already_recorded';
      elsif v_conflicts is not null then
        v_eval.o_status := 'unresolved';
        v_eval.o_reason := 'historical_scene_conflict';
      end if;
    end if;
    calendar_row_id := v_eval.o_calendar_row_id;
    raw_gym_key := v_eval.o_raw_gym_key;
    tenant_id := v_eval.o_tenant;
    group_key := v_eval.o_group_key;
    persisted_group_key := v_eval.o_persisted_group_key;
    post_date := v_eval.o_post_date;
    channel := v_eval.o_channel;
    published_at := v_eval.o_published_at;
    backfill_status := v_eval.o_status;
    reason := v_eval.o_reason;
    candidate_id := v_eval.o_candidate_id;
    phash := v_eval.o_phash;
    fingerprint := v_eval.o_fingerprint;
    exact_url := v_eval.o_exact_url;
    object_role := v_eval.o_object_role;
    source_url := v_eval.o_source_url;
    source_fingerprint := v_eval.o_source_fingerprint;
    conflicts := coalesce(v_conflicts,'[]'::jsonb);
    return next;
  end loop;
end;
$$;

-- The shared component locker waits. That is correct for ordinary writers that
-- do not yet own later locks, but activation reaches this additive hook while
-- its older receipt RPC already owns proof relation barriers. Waiting here on
-- an exact-object prepare that owns the component and is about to write those
-- relations would invert the lock graph. Preserve the same sorted component
-- closure and recheck, but fail this whole fresh transaction with 55P03.
create or replace function public.visual_scene_history_lock_components_nowait(
  p_targets jsonb
) returns void language plpgsql security definer set search_path = public as $$
declare v_members jsonb; v_recheck jsonb; v_item jsonb;
begin
  select coalesce(jsonb_agg(to_jsonb(component_group)
      order by component_group.gym_id,component_group.group_key),'[]'::jsonb)
    into v_members from (
      select distinct g.gym_id,g.group_key
      from jsonb_to_recordset(p_targets) t(gym_id text,group_key text)
      cross join lateral public.visual_group_scene_members(
        t.gym_id,t.group_key) m(group_key)
      join public.visual_group g
        on g.gym_id=t.gym_id and g.group_key=m.group_key
      where t.gym_id is not null and t.group_key is not null) component_group;
  for v_item in select value from jsonb_array_elements(v_members) loop
    begin
      perform 1 from public.visual_group
        where gym_id=v_item->>'gym_id' and group_key=v_item->>'group_key'
        for update nowait;
    exception when lock_not_available then
      raise exception 'scene component busy; retry transaction'
        using errcode='55P03';
    end;
  end loop;
  select coalesce(jsonb_agg(to_jsonb(component_group)
      order by component_group.gym_id,component_group.group_key),'[]'::jsonb)
    into v_recheck from (
      select distinct g.gym_id,g.group_key
      from jsonb_to_recordset(p_targets) t(gym_id text,group_key text)
      cross join lateral public.visual_group_scene_members(
        t.gym_id,t.group_key) m(group_key)
      join public.visual_group g
        on g.gym_id=t.gym_id and g.group_key=m.group_key
      where t.gym_id is not null and t.group_key is not null) component_group;
  if v_recheck is distinct from v_members then
    raise exception 'scene component changed while locking; retry transaction'
      using errcode='55P03';
  end if;
end;
$$;

-- Locked writer used by both the owner-only wrapper and the activation receipt
-- trigger. The JSON plan preserves the full pre-lock row and proof records.
-- Both are reloaded under the calendar row lock after component+fleet locking;
-- any changed proof field is unresolved and never written.
create or replace function public.visual_scene_history_backfill_locked(
  p_tenant text default null
) returns jsonb language plpgsql security definer set search_path = public as $$
declare
  v_row public.content_calendar%rowtype;
  v_live public.content_calendar%rowtype;
  v_eval record;
  v_plan jsonb;
  v_plans jsonb := '[]'::jsonb;
  v_components jsonb := '[]'::jsonb;
  v_results jsonb := '[]'::jsonb;
  v_conflicts jsonb;
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
      public.visual_global_object_lineage
      in share row exclusive mode nowait;
  exception when lock_not_available then
    raise exception 'scene proof writer busy; retry transaction'
      using errcode='55P03';
  end;
  if not pg_try_advisory_xact_lock(hashtextextended(
      jsonb_build_array('visual_scene_global')::text,0)) then
    raise exception 'global scene claim busy; retry transaction'
      using errcode='55P03';
  end if;

  for v_plan in select value from jsonb_array_elements(v_plans)
  loop
    -- Reload the LIVE row after the complete lock set. Never re-evaluate the
    -- stale loop record used to plan component locks.
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

    -- Full proof-field equality: the entire live calendar composite and every
    -- evaluator output (tenant/group/date/candidate/pHash/fingerprints/URLs/
    -- roles/source/channel/publication markers) must match the locked plan.
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

    if exists(select 1 from public.visual_scene_phash_occupied o
        where o.phash=v_eval.o_phash and o.tenant_id=v_eval.o_tenant
          and o.group_key=v_eval.o_group_key
          and o.used_date=v_eval.o_post_date) then
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

  return jsonb_build_object(
    'scope',case when p_tenant is null then 'fleet' else 'tenant' end,
    'tenant_id',p_tenant,
    'transaction_id',txid_current(),
    'covered_published_rows',jsonb_array_length(v_plans),
    'barrier','content_calendar share row exclusive',
    'lock_order','calendar barrier and rows; all scene components nowait; proof relation barriers nowait; fleet scene lock',
    'inserted',v_inserted,'already_recorded',v_already,
    'unresolved',v_unresolved,'rows',v_results);
end;
$$;

-- Replace the frozen wave's 0A000 owner-only stub without changing its return
-- type. Detailed results are persisted in activation proof and available from
-- the internal locked helper; this compatibility wrapper returns insert count.
create or replace function public.visual_scene_backfill_occupied()
returns integer language plpgsql security definer set search_path = public as $$
declare v_receipt jsonb;
begin
  v_receipt := public.visual_scene_history_backfill_locked(null);
  return (v_receipt->>'inserted')::integer;
end;
$$;

-- Same-transaction activation hook. It runs only while the activation RPC's
-- calendar write barrier is held, rejects every unresolved historical row,
-- and enriches the receipt before the arm guard sees it.
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
  -- INSERT .. ON CONFLICT DO UPDATE fires the BEFORE INSERT trigger and then
  -- the BEFORE UPDATE trigger. The excluded proof reaching the update already
  -- carries the first, same-transaction locked receipt; preserve it instead
  -- of running the backfill twice and replacing `inserted` with `already`.
  if new.proof ? 'scene_history' then
    if coalesce(new.proof->'scene_history'->>'scope','') <> 'fleet'
        or coalesce((new.proof->'scene_history'->>'transaction_id')::bigint,-1)
          <> txid_current()
        or coalesce((new.proof->'scene_history'->>'unresolved')::integer,-1) <> 0 then
      raise exception 'activation refused: invalid scene history receipt'
        using errcode='23514';
    end if;
    return new;
  end if;
  -- Activation is a fleet authority. A tenant-scoped receipt cannot establish
  -- that another tenant's unknown/source-null history is absent.
  v_receipt := public.visual_scene_history_backfill_locked(null);
  if (v_receipt->>'unresolved')::integer <> 0 then
    raise exception 'activation refused: unresolved scene history requires review'
      using errcode='23514', detail=v_receipt::text;
  end if;
  new.proof := coalesce(new.proof,'{}'::jsonb) ||
    jsonb_build_object('scene_history',v_receipt);
  return new;
end;
$$;

drop trigger if exists visual_scene_history_activation_receipt
  on public.visual_group_activation;
create trigger visual_scene_history_activation_receipt
  before insert or update on public.visual_group_activation
  for each row execute function public.visual_scene_history_activation_receipt();

revoke all on function public.visual_scene_history_evaluate(public.content_calendar)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_scene_history_lock_components_nowait(jsonb)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_scene_history_backfill_locked(text)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_scene_backfill_occupied()
  from public,anon,authenticated,service_role;
revoke all on function public.visual_scene_history_activation_receipt()
  from public,anon,authenticated,service_role;
revoke all on function public.visual_scene_history_audit(text)
  from public,anon,authenticated;
grant execute on function public.visual_scene_history_audit(text)
  to service_role;

commit;
