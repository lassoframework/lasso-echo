-- DRAFT / UNAPPLIED / OFF -- DO NOT APPLY OR ACTIVATE.
--
-- Atomic writer boundary for scene candidates and new content_calendar rows.
-- Apply only after DRAFT_visual_scene_claim_wave_20261003.sql.  The Python
-- caller remains behind AGENT_VISUAL_SCENE_REGISTER (explicit-on, default OFF).
--
-- One RPC invocation is one PostgreSQL statement transaction.  For each input
-- item this function:
--   1. resolves the raw calendar/account alias to one canonical tenant;
--   2. validates exact displayed URL, display/poster role, md5 fingerprint and
--      evidence.verified_bytes against the calendar row;
--   3. stages the candidate (never use/occupancy) BEFORE the row INSERT so the
--      authoritative BEFORE trigger can bind it; and
--   4. inserts the row and returns its persisted post-trigger state.
-- Any candidate N or row N error escapes the function and rolls back every
-- earlier candidate and row in the batch.  There is no compensation DELETE.
--
-- Media PATCHes are deliberately outside this insert-only RPC.  Existing
-- patch_image_url uses a server-side compare-and-swap filter; atomic candidate
-- staging for that CAS path remains a separate activation prerequisite.
--
-- Rollback before activation:
--   drop function if exists public.visual_scene_insert_calendar_batch(text,jsonb,text);
--   drop index if exists public.visual_scene_candidate_canonical_binding_uq;
--   drop table if exists public.visual_scene_calendar_install_receipt;
--   grant execute on function public.visual_scene_register_candidate(
--     text,text,text,text,text,jsonb,text,text) to service_role;

begin;

-- Freeze the candidate/hold inventory for the complete preflight + index
-- installation transaction.  No concurrent direct registration can appear
-- between the receipt and the unique index.
lock table public.visual_scene_candidate in share mode;
lock table public.visual_scene_review_hold in share mode;

-- Refuse every pre-existing duplicate before changing ACLs, functions, or
-- indexes.  The DETAIL is the remediation inventory: exact candidate IDs,
-- identity/evidence classification, and every review_hold FK that would need
-- an explicitly reviewed rebind. Contradictory identities/evidence are never
-- merged automatically.
do $$
declare v_duplicates jsonb;
begin
  with grouped as (
    select c.tenant_id, c.group_key, c.object_role, c.exact_url,
      array_agg(c.candidate_id order by c.candidate_id) as candidate_ids,
      count(distinct (btrim(c.phash::text), c.fingerprint)) as identity_variants,
      count(distinct md5(c.evidence::text)) as evidence_variants,
      jsonb_agg(jsonb_build_object(
        'candidate_id', c.candidate_id,
        'phash', btrim(c.phash::text),
        'fingerprint', c.fingerprint,
        'evidence_md5', md5(c.evidence::text),
        'evidence', c.evidence
      ) order by c.candidate_id) as candidates
    from public.visual_scene_candidate c
    group by c.tenant_id, c.group_key, c.object_role, c.exact_url
    having count(*) > 1
  ), inventory as (
    select g.*,
      case
        when g.identity_variants > 1 then 'conflicting_identity'
        when g.evidence_variants > 1 then 'conflicting_evidence'
        else 'identical_evidence'
      end as classification,
      coalesce((select jsonb_agg(jsonb_build_object(
          'hold_id', h.hold_id, 'candidate_id', h.candidate_id,
          'state', h.state, 'claim_date', h.claim_date
        ) order by h.hold_id)
        from public.visual_scene_review_hold h
        where h.candidate_id = any(g.candidate_ids)), '[]'::jsonb) as review_hold_fks
    from grouped g
  )
  select jsonb_agg(jsonb_build_object(
      'tenant_id', tenant_id, 'group_key', group_key,
      'object_role', object_role, 'exact_url', exact_url,
      'classification', classification, 'candidate_ids', candidate_ids,
      'candidates', candidates, 'review_hold_fks', review_hold_fks
    ) order by tenant_id, group_key, object_role, exact_url)
    into v_duplicates from inventory;
  if v_duplicates is not null then
    raise exception 'atomic scene calendar install refused: duplicate candidate bindings require review'
      using errcode = '23514', detail = v_duplicates::text,
        hint = 'Use DRAFT_visual_scene_calendar_candidate_dedupe_20261004.sql only for explicitly reviewed identical_evidence pairs; conflicting evidence/identity requires source-level adjudication.';
  end if;
end;
$$;

create table if not exists public.visual_scene_calendar_install_receipt (
  receipt_id uuid primary key default gen_random_uuid(),
  candidate_count bigint not null,
  candidate_snapshot_md5 char(32) not null,
  duplicate_binding_count bigint not null check (duplicate_binding_count = 0),
  reviewed_by text not null check (btrim(reviewed_by) <> ''),
  reviewed_at timestamptz not null default now()
);
revoke all on public.visual_scene_calendar_install_receipt
  from public, anon, authenticated, service_role;
grant select on public.visual_scene_calendar_install_receipt to service_role;

insert into public.visual_scene_calendar_install_receipt
  (candidate_count, candidate_snapshot_md5, duplicate_binding_count, reviewed_by)
select count(*), md5(coalesce(string_agg(
    c.candidate_id::text || '|' || c.tenant_id || '|' || c.group_key || '|' ||
    c.object_role || '|' || btrim(c.phash::text) || '|' || c.exact_url || '|' ||
    c.fingerprint || '|' || md5(c.evidence::text) || '|' || c.attested_by,
    E'\n' order by c.candidate_id), '')), 0, 'atomic-calendar-install-preflight'
from public.visual_scene_candidate c;

-- The frozen candidate resolver binds a row by tenant/group/role/exact URL and
-- returns the lowest UUID when identical pHashes were inserted more than once.
-- Random duplicate UUIDs therefore make row binding nondeterministic.  Make
-- that resolver key canonical: one candidate row owns one exact displayed
-- object binding.  A changed pHash/fingerprint on the same binding is an
-- identity conflict and must fail closed rather than replacing evidence.
-- IF NOT EXISTS alone accepts a same-named wrong-shape index. Validate the
-- catalog before and after creation so replay either proves the exact unique
-- btree key or refuses without changing the existing object.
do $$
declare v_valid boolean;
begin
  if to_regclass('public.visual_scene_candidate_canonical_binding_uq') is not null then
    select i.indisunique and i.indisvalid and i.indisready
      and i.indpred is null and i.indexprs is null
      and am.amname = 'btree'
      and (select array_agg(a.attname order by k.ordinality)
           from unnest(string_to_array(i.indkey::text, ' ')::smallint[])
                with ordinality as k(attnum, ordinality)
           join pg_attribute a on a.attrelid = i.indrelid and a.attnum = k.attnum)
          = array['tenant_id','group_key','object_role','exact_url']::name[]
      into v_valid
      from pg_index i
      join pg_class ic on ic.oid = i.indexrelid
      join pg_namespace n on n.oid = ic.relnamespace
      join pg_class tc on tc.oid = i.indrelid
      join pg_am am on am.oid = ic.relam
      where n.nspname = 'public'
        and ic.relname = 'visual_scene_candidate_canonical_binding_uq'
        and tc.oid = 'public.visual_scene_candidate'::regclass;
    if coalesce(v_valid, false) is not true then
      raise exception 'visual_scene_candidate_canonical_binding_uq exists with wrong catalog shape'
        using errcode = '42P07';
    end if;
  end if;
end;
$$;

create unique index if not exists visual_scene_candidate_canonical_binding_uq
  on public.visual_scene_candidate using btree
  (tenant_id, group_key, object_role, exact_url);

do $$
declare v_valid boolean;
begin
  select i.indisunique and i.indisvalid and i.indisready
    and i.indpred is null and i.indexprs is null
    and am.amname = 'btree'
    and (select array_agg(a.attname order by k.ordinality)
         from unnest(string_to_array(i.indkey::text, ' ')::smallint[])
              with ordinality as k(attnum, ordinality)
         join pg_attribute a on a.attrelid = i.indrelid and a.attnum = k.attnum)
        = array['tenant_id','group_key','object_role','exact_url']::name[]
    into v_valid
    from pg_index i
    join pg_class ic on ic.oid = i.indexrelid
    join pg_namespace n on n.oid = ic.relnamespace
    join pg_am am on am.oid = ic.relam
    where n.nspname = 'public'
      and ic.relname = 'visual_scene_candidate_canonical_binding_uq'
      and i.indrelid = 'public.visual_scene_candidate'::regclass;
  if coalesce(v_valid, false) is not true then
    raise exception 'visual_scene_candidate_canonical_binding_uq was not installed with required shape'
      using errcode = '42P07';
  end if;
end;
$$;

-- The frozen inventory remained locked throughout preflight and index build.
-- Require the current state to match the latest zero-duplicate receipt before
-- changing the direct-registration ACL or replacing the writer function.
do $$
declare v_count bigint; v_snapshot char(32);
begin
  select count(*), md5(coalesce(string_agg(
      c.candidate_id::text || '|' || c.tenant_id || '|' || c.group_key || '|' ||
      c.object_role || '|' || btrim(c.phash::text) || '|' || c.exact_url || '|' ||
      c.fingerprint || '|' || md5(c.evidence::text) || '|' || c.attested_by,
      E'\n' order by c.candidate_id), ''))
    into v_count, v_snapshot from public.visual_scene_candidate c;
  if not exists(select 1 from public.visual_scene_calendar_install_receipt r
      where r.duplicate_binding_count = 0 and r.candidate_count = v_count
        and r.candidate_snapshot_md5 = v_snapshot) then
    raise exception 'atomic scene calendar install lacks a matching zero-duplicate prerequisite receipt'
      using errcode = '23514';
  end if;
end;
$$;

comment on index public.visual_scene_candidate_canonical_binding_uq is
  'DRAFT/UNAPPLIED/OFF: canonical stable candidate per tenant/group/displayed role/exact URL; identical retries reuse its UUID and contradictory identity fails closed.';

-- The frozen registration RPC always INSERTs a fresh random UUID.  Keeping it
-- directly executable by service_role would bypass canonical reuse and race
-- the calendar transaction.  This additive draft leaves the frozen function
-- source untouched, revokes that direct route, and exposes registration only
-- through the atomic calendar RPC below.  The function owner retains implicit
-- EXECUTE, so this SECURITY DEFINER wrapper can call it after validation.
revoke execute on function public.visual_scene_register_candidate(
  text,text,text,text,text,jsonb,text,text) from service_role;

create or replace function public.visual_scene_insert_calendar_batch(
  p_account_key text,
  p_items jsonb,
  p_actor text default 'visual_writer_prepare'
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_account_tenant text;
  v_item jsonb;
  v_row jsonb;
  v_candidate jsonb;
  v_evidence jsonb;
  v_inserted jsonb;
  v_results jsonb := '[]'::jsonb;
  v_candidate_id uuid;
  v_candidate_reused boolean;
  v_existing_phash text;
  v_existing_fingerprint text;
  v_bound_candidate_id uuid;
  v_row_id uuid;
  v_row_tenant text;
  v_candidate_tenant text;
  v_group text;
  v_role text;
  v_exact_url text;
  v_inserted_role text;
  v_inserted_exact_url text;
  v_image_url text;
  v_thumbnail_url text;
  v_phash text;
  v_fingerprint text;
  v_columns text;
  v_values text;
  v_ordinal bigint;
begin
  -- Deployment-order or privilege drift must not reopen the random-UUID
  -- registration route beside this canonical writer.
  if has_function_privilege(
      'service_role',
      'public.visual_scene_register_candidate(text,text,text,text,text,jsonb,text,text)',
      'EXECUTE') then
    raise exception 'unsafe direct service-role scene registration is enabled'
      using errcode = '42501';
  end if;
  if nullif(btrim(p_account_key), '') is null
      or nullif(btrim(p_actor), '') is null then
    raise exception 'atomic scene calendar insert needs account key and actor'
      using errcode = '22023';
  end if;
  if p_items is null or jsonb_typeof(p_items) <> 'array'
      or jsonb_array_length(p_items) = 0
      or jsonb_array_length(p_items) > 500 then
    raise exception 'atomic scene calendar insert needs 1..500 items'
      using errcode = '22023';
  end if;

  -- p_account_key may be a raw/current/legacy calendar alias.  All internal
  -- candidate identities below use only this resolved canonical tenant.
  v_account_tenant := public.visual_group_tenant_strict(p_account_key)::text;

  for v_item, v_ordinal in
    select value, ordinality
      from jsonb_array_elements(p_items) with ordinality
  loop
    if jsonb_typeof(v_item) <> 'object'
        or jsonb_typeof(v_item->'calendar_row') <> 'object' then
      raise exception 'atomic scene calendar item % is invalid', v_ordinal
        using errcode = '22023';
    end if;
    v_row := v_item->'calendar_row';
    v_candidate := v_item->'candidate';

    -- The Python store force-writes the raw account key on every row.  Require
    -- that exact value here as a second tenant-isolation barrier, then resolve
    -- it so an alias and its canonical candidate identity can safely meet.
    if v_row->>'gym_id' is distinct from p_account_key then
      raise exception 'calendar row % escaped its account scope', v_ordinal
        using errcode = '42501';
    end if;
    v_row_tenant := public.visual_group_tenant_strict(v_row->>'gym_id')::text;
    if v_row_tenant is distinct from v_account_tenant then
      raise exception 'calendar row % resolves to another tenant', v_ordinal
        using errcode = '42501';
    end if;

    -- Allocate the row id inside this transaction before staging.  The id is
    -- embedded in immutable candidate evidence, yet no calendar trigger runs
    -- until after registration.
    if nullif(btrim(v_row->>'id'), '') is null then
      v_row_id := gen_random_uuid();
      v_row := jsonb_set(v_row, '{id}', to_jsonb(v_row_id::text), true);
    else
      begin
        v_row_id := (v_row->>'id')::uuid;
      exception when invalid_text_representation then
        raise exception 'calendar row % id is not a UUID', v_ordinal
          using errcode = '22023';
      end;
      v_row := jsonb_set(v_row, '{id}', to_jsonb(v_row_id::text), true);
    end if;

    v_image_url := nullif(btrim(v_row->>'image_url'), '');
    v_thumbnail_url := nullif(btrim(v_row->>'thumbnail_url'), '');
    if v_thumbnail_url is not null and v_thumbnail_url is distinct from v_image_url then
      v_role := 'poster';
      v_exact_url := v_thumbnail_url;
    elsif v_image_url is not null then
      v_role := 'display';
      v_exact_url := v_image_url;
    else
      v_role := null;
      v_exact_url := null;
    end if;

    v_candidate_id := null;
    v_candidate_reused := false;
    if v_candidate is null or v_candidate = 'null'::jsonb then
      if v_exact_url is not null then
        raise exception 'calendar row % has displayed media but no scene candidate', v_ordinal
          using errcode = '23514';
      end if;
    else
      if jsonb_typeof(v_candidate) <> 'object'
          or v_candidate->>'kind' is distinct from 'visual_scene_candidate'
          or v_candidate->>'stage' is distinct from 'candidate'
          or v_candidate->'usage_claimed' is distinct from 'false'::jsonb
          or v_candidate->'counts_as_use' is distinct from 'false'::jsonb then
        raise exception 'calendar row % candidate is not staging-only evidence', v_ordinal
          using errcode = '22023';
      end if;
      if v_exact_url is null then
        raise exception 'calendar row % candidate has no displayed object', v_ordinal
          using errcode = '23514';
      end if;
      v_candidate_tenant := public.visual_group_tenant_strict(
        v_candidate->>'tenant_id')::text;
      if v_candidate_tenant is distinct from v_row_tenant then
        raise exception 'calendar row % candidate belongs to another tenant', v_ordinal
          using errcode = '42501';
      end if;
      v_group := nullif(btrim(v_candidate->>'group_key'), '');
      if v_group is null
          or v_row->>'visual_group_key' is distinct from v_group then
        raise exception 'calendar row % candidate does not match its visual group', v_ordinal
          using errcode = '23514';
      end if;
      if v_candidate->>'object_role' is distinct from v_role
          or v_candidate->>'exact_url' is distinct from v_exact_url then
        raise exception 'calendar row % candidate does not bind its exact displayed object', v_ordinal
          using errcode = '23514';
      end if;
      v_phash := lower(btrim(v_candidate->>'phash'));
      v_fingerprint := lower(btrim(v_candidate->>'fingerprint'));
      v_evidence := v_candidate->'evidence';
      if v_phash is null or v_phash !~ '^[0-9a-f]{16}$'
          or v_fingerprint is null or v_fingerprint !~ '^md5:[0-9a-f]{32}$'
          or v_evidence is null or jsonb_typeof(v_evidence) <> 'object'
          or lower(btrim(v_evidence->>'verified_bytes')) is distinct from v_fingerprint then
        raise exception 'calendar row % candidate lacks exact verified byte evidence', v_ordinal
          using errcode = '22023';
      end if;

      -- Server-owned fields are written last and cannot be overridden by the
      -- JSON caller.  Registration verifies the same URL+fingerprint against
      -- visual_global_object_attestation and writes no occupancy/use state.
      v_evidence := v_evidence || jsonb_build_object(
        'verified_bytes', v_fingerprint,
        'tenant_id', v_row_tenant,
        'group_key', v_group,
        'object_role', v_role,
        'exact_url', v_exact_url,
        'phash', v_phash,
        'calendar_row_id', v_row_id::text,
        'first_calendar_row_id', v_row_id::text,
        'registration_provenance', 'first_atomic_calendar_registration',
        'atomic_calendar_write', true
      );
      -- Reuse the one canonical candidate for this exact displayed binding.
      -- The unique index serializes concurrent first writers.  If two callers
      -- both see no row, the loser waits on the winner's index entry, catches
      -- unique_violation in this subtransaction, and then reads the committed
      -- winner.  Reuse is allowed only for the exact same pHash+fingerprint.
      select c.candidate_id, btrim(c.phash::text), c.fingerprint
        into v_candidate_id, v_existing_phash, v_existing_fingerprint
        from public.visual_scene_candidate c
        where c.tenant_id = v_row_tenant and c.group_key = v_group
          and c.object_role = v_role and c.exact_url = v_exact_url;
      v_candidate_reused := v_candidate_id is not null;
      if v_candidate_id is null then
        begin
          v_candidate_id := public.visual_scene_register_candidate(
            v_row_tenant, v_group, v_phash, v_exact_url, v_fingerprint,
            v_evidence, btrim(p_actor), v_role
          );
          v_existing_phash := v_phash;
          v_existing_fingerprint := v_fingerprint;
          v_candidate_reused := false;
        exception when unique_violation then
          v_candidate_id := null;
          select c.candidate_id, btrim(c.phash::text), c.fingerprint
            into v_candidate_id, v_existing_phash, v_existing_fingerprint
            from public.visual_scene_candidate c
            where c.tenant_id = v_row_tenant and c.group_key = v_group
              and c.object_role = v_role and c.exact_url = v_exact_url;
          if v_candidate_id is null then
            raise;
          end if;
          v_candidate_reused := true;
        end;
      end if;
      if v_candidate_id is null then
        raise exception 'calendar row % candidate registration returned null', v_ordinal
          using errcode = '23514';
      end if;
      if v_existing_phash is distinct from v_phash
          or v_existing_fingerprint is distinct from v_fingerprint then
        raise exception 'calendar row % conflicts with canonical scene candidate identity', v_ordinal
          using errcode = '23514';
      end if;
    end if;

    -- Use only real, writable content_calendar column names.  Identifiers come
    -- from pg_attribute and are quoted with format(%I), so SECURITY DEFINER
    -- cannot be turned into arbitrary SQL.  The key-specific INSERT preserves
    -- table defaults for omitted columns, matching the normal REST insert.
    if v_row ? 'scene_candidate' or exists (
      select 1 from jsonb_object_keys(v_row) as supplied(key)
      where not exists (
        select 1 from pg_attribute a
        where a.attrelid = 'public.content_calendar'::regclass
          and a.attname = supplied.key and a.attnum > 0 and not a.attisdropped
          and a.attgenerated = '' and a.attidentity = ''
      )
    ) then
      raise exception 'calendar row % contains an unknown or unwritable column', v_ordinal
        using errcode = '42703';
    end if;
    select
      string_agg(format('%I', supplied.key), ', ' order by a.attnum),
      string_agg(format('r.%I', supplied.key), ', ' order by a.attnum)
      into v_columns, v_values
      from jsonb_object_keys(v_row) as supplied(key)
      join pg_attribute a
        on a.attrelid = 'public.content_calendar'::regclass
       and a.attname = supplied.key and a.attnum > 0 and not a.attisdropped
       and a.attgenerated = '' and a.attidentity = '';
    if v_columns is null then
      raise exception 'calendar row % has no writable columns', v_ordinal
        using errcode = '22023';
    end if;

    execute format(
      'insert into public.content_calendar as cc (%s) '
      'select %s from jsonb_populate_record(null::public.content_calendar, $1) as r '
      'returning to_jsonb(cc)',
      v_columns, v_values
    ) using v_row into v_inserted;

    if v_inserted is null
        or v_inserted->>'id' is distinct from v_row_id::text
        or v_inserted->>'gym_id' is distinct from p_account_key then
      raise exception 'calendar row % insert returned unverified state', v_ordinal
        using errcode = '23514';
    end if;

    -- Rebind against the PERSISTED post-trigger row. A trigger may normalize
    -- its visual group or media fields, and visual_scene_row_candidate may
    -- resolve another previously staged candidate. Never report success unless
    -- the exact candidate registered above remains the candidate for the row
    -- that actually committed.
    if v_candidate_id is not null then
      select public.visual_scene_row_candidate(r), d.object_role, d.exact_url
        into v_bound_candidate_id, v_inserted_role, v_inserted_exact_url
        from jsonb_populate_record(null::public.content_calendar, v_inserted) as r
        left join lateral public.visual_scene_row_delivered_object(r) d on true;
      if v_bound_candidate_id is distinct from v_candidate_id
          or v_inserted->>'visual_group_key' is distinct from v_group
          or v_inserted_role is distinct from v_role
          or v_inserted_exact_url is distinct from v_exact_url then
        raise exception 'calendar row % persisted candidate binding changed', v_ordinal
          using errcode = '23514';
      end if;
    end if;
    v_results := v_results || jsonb_build_array(jsonb_build_object(
      'input_index', v_ordinal - 1,
      'candidate_id', v_candidate_id,
      'candidate_binding', case when v_candidate_id is null then null else
        jsonb_build_object(
          'candidate_id', v_candidate_id,
          'registration_reused', v_candidate_reused,
          'tenant_id', v_row_tenant,
          'group_key', v_group,
          'object_role', v_inserted_role,
          'exact_url', v_inserted_exact_url,
          'phash', v_phash,
          'fingerprint', v_fingerprint
        ) end,
      'disposition', case
        when v_inserted->>'status' = 'pending'
         and v_inserted->>'variant_status' = 'archived'
         and v_inserted->>'media_not_ready_reason' = 'scene_review_hold'
         and v_inserted->>'publish_claim_token' is null
         and v_inserted->>'publish_reservation_day' is null
        then 'scene_review_hold'
        else 'inserted'
      end,
      'row', v_inserted
    ));
  end loop;
  return v_results;
end;
$$;

revoke all on function public.visual_scene_insert_calendar_batch(text,jsonb,text)
  from public, anon, authenticated;
grant execute on function public.visual_scene_insert_calendar_batch(text,jsonb,text)
  to service_role;

commit;
