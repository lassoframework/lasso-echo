-- DRAFT / UNAPPLIED. PR235 global writer integration (Child B).
-- Evolves the APPLIED public.stage_lasso_campaign_row (see
-- migrations/lasso_campaign_stage_20260923.sql) with two NARROW additions:
-- optional owner-attested visual identity (p_visual_group_key, p_byte_hash)
-- produced by agent.visual_writer_prepare.prepare_same_object for the exact
-- reviewed artifact URL.  Everything else — the 12-key p_row allowlist, the
-- artifact/provenance checks, the logical-slot and cross-day guards — is
-- unchanged, and callers that supply only the original five arguments get
-- byte-for-byte the legacy behavior (both new parameters default to null).
--
-- Dependencies (draft stack, apply order):
--   migrations/lasso_campaign_stage_20260923.sql      (applied base RPC)
--   migrations/DRAFT_visual_group_schema_20261002.sql (visual_group*, tenant fns)
--   migrations/DRAFT_visual_group_claim_trigger_20261002.sql
--   migrations/DRAFT_visual_global_history_20261002.sql
--     (visual_global_row_bytes_verified, attestation/member/usage tables)
-- Rollback: drop the 7-argument function and recreate the 5-argument one from
-- migrations/lasso_campaign_stage_20260923.sql.

-- The two identity columns the armed visual guard reads.  visual_group_key is
-- already added by DRAFT_visual_group_schema_20261002.sql; both are idempotent.
alter table public.content_calendar add column if not exists visual_group_key text;
alter table public.content_calendar add column if not exists byte_hash text;

-- Replacing the signature would leave the unguarded 5-argument overload alive;
-- drop it.  Calls with five arguments still bind to the evolved function via
-- the defaulted parameters below.
drop function if exists public.stage_lasso_campaign_row(jsonb, text, text, text, text);

create or replace function public.stage_lasso_campaign_row(
  p_row jsonb,
  p_artifact_tenant text,
  p_source_hash text,
  p_policy_version text,
  p_image_sha256 text,
  p_visual_group_key text default null,
  p_byte_hash text default null
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_id uuid;
  v_day date;
  v_account text;
  v_slot integer;
  v_image_url text;
  v_caption text;
  v_scheduled_at timestamptz;
  v_existing public.content_calendar%rowtype;
  v_tenant text;
  v_candidate public.content_calendar;
  v_fingerprints text[];
  v_rechecked_fingerprints text[];
  v_fingerprint text;
begin
  if p_row is null or jsonb_typeof(p_row) <> 'object' then
    return jsonb_build_object('result', 'conflict', 'reason', 'invalid_row');
  end if;

  -- Reject invented/unknown provenance and every calendar field this narrow
  -- staging path is not allowed to write.  The allowlist is unchanged; the
  -- visual identity travels as dedicated parameters, never inside p_row.
  if exists (
    select 1 from jsonb_object_keys(p_row) as supplied(key)
    where supplied.key not in (
      'id', 'gym_id', 'account', 'post_date', 'pillar', 'format', 'caption',
      'image_url', 'status', 'scheduled_at', 'slot_index', 'variant_status'
    )
  ) then
    return jsonb_build_object('result', 'conflict', 'reason', 'unsupported_row_key');
  end if;

  begin
    v_id := nullif(p_row->>'id', '')::uuid;
    v_day := nullif(p_row->>'post_date', '')::date;
    v_slot := nullif(p_row->>'slot_index', '')::integer;
    v_scheduled_at := nullif(p_row->>'scheduled_at', '')::timestamptz;
  exception when others then
    return jsonb_build_object('result', 'conflict', 'reason', 'invalid_typed_value');
  end;
  v_account := lower(btrim(coalesce(p_row->>'account', '')));
  v_image_url := btrim(coalesce(p_row->>'image_url', ''));
  v_caption := coalesce(p_row->>'caption', '');

  if v_id is null
      or v_day is null
      or p_row->>'gym_id' is distinct from 'lasso'
      or v_day not between date '2026-09-23' and date '2026-11-08'
      or v_account not in ('instagram', 'facebook')
      or lower(btrim(coalesce(p_row->>'format', ''))) <> 'feed'
      or lower(btrim(coalesce(p_row->>'status', ''))) <> 'pending'
      or lower(btrim(coalesce(p_row->>'variant_status', ''))) <> 'active'
      or v_slot is null
      or v_slot not in (0, 1, 2)
      or nullif(btrim(coalesce(p_row->>'pillar', '')), '') is null
      or nullif(v_caption, '') is null
      or nullif(v_image_url, '') is null
      or nullif(btrim(coalesce(p_artifact_tenant, '')), '') is null
      or p_artifact_tenant not in ('lasso', 'lasso_ig')
      or p_source_hash is null
      or p_source_hash !~ '^[0-9a-f]{64}$'
      or p_image_sha256 is null
      or p_image_sha256 !~ '^[0-9a-f]{64}$'
      or nullif(btrim(coalesce(p_policy_version, '')), '') is null then
    return jsonb_build_object('result', 'conflict', 'reason', 'out_of_scope');
  end if;
  if (v_slot = 2) <> (lower(btrim(p_row->>'pillar')) = 'summit') then
    return jsonb_build_object('result', 'conflict', 'reason', 'pillar_slot_mismatch');
  end if;
  if p_visual_group_key is not null
      and current_setting('transaction_isolation') <> 'read committed' then
    return jsonb_build_object('result', 'conflict', 'reason', 'visual_identity_requires_read_committed');
  end if;

  -- Take the calendar write barrier before reading mutable scene membership or
  -- global usage. A concurrent calendar claim/import must finish first, then
  -- these checks see its committed state under READ COMMITTED.
  lock table public.content_calendar in share row exclusive mode;

  -- Visual identity is all-or-nothing and must be the exact owner-attested
  -- same-object preparation for THIS delivered URL: the group must belong to
  -- the canonical lasso tenant, the staged row identity must pass the global
  -- exact-byte authority (attested source+delivered members, matching derived
  -- byte hash). Existing scene usage must have this same tenant and date.
  -- Anything short of that fails closed; group/hash hints are never trusted.
  if (p_visual_group_key is null) <> (p_byte_hash is null) then
    return jsonb_build_object('result', 'conflict', 'reason', 'invalid_visual_identity');
  end if;
  if p_visual_group_key is not null then
    v_tenant := public.visual_group_tenant_id('lasso')::text;
    if v_tenant is null
        or p_visual_group_key !~ '^vg_[A-Za-z0-9_-]{1,120}$'
        or lower(btrim(p_byte_hash)) !~ '^derived:md5:[0-9a-f]{32}$'
        or not exists (select 1 from public.visual_group
            where gym_id = v_tenant and group_key = p_visual_group_key) then
      return jsonb_build_object('result', 'conflict', 'reason', 'unknown_visual_scene');
    end if;
    v_candidate.gym_id := 'lasso';
    v_candidate.image_url := v_image_url;
    v_candidate.visual_group_key := p_visual_group_key;
    v_candidate.byte_hash := lower(btrim(p_byte_hash));
    if not public.visual_global_row_bytes_verified(v_candidate) then
      return jsonb_build_object('result', 'conflict', 'reason', 'visual_bytes_not_attested');
    end if;
    select array_agg(f.fingerprint order by f.fingerprint) into v_fingerprints
      from public.visual_global_scene_fingerprints(v_tenant, p_visual_group_key) f;
    if coalesce(cardinality(v_fingerprints), 0) = 0 then
      return jsonb_build_object('result', 'conflict', 'reason', 'visual_bytes_not_attested');
    end if;
    -- Match the authoritative claim's sorted fingerprint locks. The private
    -- helper uses a try-lock while this function holds the calendar barrier;
    -- a busy key raises 55P03 for a fresh-transaction retry, never deadlocks.
    foreach v_fingerprint in array v_fingerprints loop
      perform public.visual_group_auxiliary_lock(hashtextextended(
        jsonb_build_array('visual_global_fingerprint', v_fingerprint)::text, 0));
    end loop;
    -- Identity can expand while acquiring those locks. Never retain this set
    -- and acquire a newly discovered lower key out of order in one transaction.
    select array_agg(f.fingerprint order by f.fingerprint) into v_rechecked_fingerprints
      from public.visual_global_scene_fingerprints(v_tenant, p_visual_group_key) f;
    if v_rechecked_fingerprints is distinct from v_fingerprints
        or not public.visual_global_row_bytes_verified(v_candidate) then
      raise exception 'visual scene changed while locking; retry transaction'
        using errcode = '55P03';
    end if;
    p_byte_hash := lower(btrim(p_byte_hash));
  end if;

  if not exists (
    select 1
      from public.echo_infographic_artifacts a
     where a.tenant = p_artifact_tenant
       and a.image_url = v_image_url
       and a.image_sha256 = p_image_sha256
       and a.evidence->>'grade_status' = 'PASS'
       and a.evidence->>'image_sha256' = p_image_sha256
       and a.evidence->>'policy_version' = p_policy_version
       and a.source_identity->>'source_hash' = p_source_hash
  ) then
    return jsonb_build_object('result', 'conflict', 'reason', 'reviewed_artifact_mismatch');
  end if;

  -- A same-date IG/FB sibling may join an existing scene. A released or
  -- different-date member, or a byte already owned by another tenant/date,
  -- cannot be staged even while this draft guard is still disarmed. Check
  -- before idempotent readback so a newly discovered conflict never looks OK.
  if p_visual_group_key is not null then
    if exists (select 1 from public.visual_global_usage_member m
        where m.tenant_id = v_tenant and m.group_key = p_visual_group_key
          and (m.state = 'released' or m.used_date is distinct from v_day))
       or exists (
        select 1 from public.visual_global_usage u
        where u.fingerprint = any(v_fingerprints)
          and (u.state = 'released' or u.tenant_id <> v_tenant
            or u.used_date is distinct from v_day)
       ) then
      return jsonb_build_object('result', 'conflict', 'reason', 'visual_usage_already_claimed');
    end if;
  end if;

  select * into v_existing
    from public.content_calendar
   where id = v_id;
  if found then
    -- Idempotent readback: a re-call with the SAME identity (now including the
    -- visual identity) succeeds; any differing field is a conflict.
    if v_existing.gym_id = 'lasso'
       and lower(btrim(coalesce(v_existing.account, ''))) = v_account
       and v_existing.post_date = v_day
       and lower(btrim(coalesce(v_existing.format, 'feed'))) = 'feed'
       and v_existing.status = 'pending'
       and v_existing.variant_status = 'active'
       and v_existing.slot_index = v_slot
       and v_existing.pillar = p_row->>'pillar'
       and v_existing.caption = v_caption
       and v_existing.image_url = v_image_url
       and v_existing.scheduled_at is not distinct from v_scheduled_at
       and v_existing.visual_group_key is not distinct from p_visual_group_key
       and v_existing.byte_hash is not distinct from p_byte_hash then
      return jsonb_build_object('result', 'idempotent', 'id', v_id);
    end if;
    return jsonb_build_object('result', 'conflict', 'reason', 'id_reused_with_different_row', 'id', v_id);
  end if;

  -- An active occupying row owns its account/day/slot. Protected rows are read
  -- as conflicts and are never modified by this function.
  if exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso'
       and c.post_date = v_day
       and lower(btrim(coalesce(c.account, ''))) = v_account
       and lower(btrim(coalesce(c.format, 'feed'))) = 'feed'
       and c.variant_status = 'active'
       and c.status in ('pending', 'approved', 'publishing', 'published', 'draft')
       -- A legacy active row with no ordinal makes the logical shape ambiguous;
       -- fail closed until it is separately normalized under review.
       and (c.slot_index = v_slot or c.slot_index is null)
  ) then
    return jsonb_build_object('result', 'conflict', 'reason', 'occupied_logical_slot');
  end if;

  -- Instagram and Facebook are the two rows of one logical creative and may
  -- share media/copy only when date and slot match. Reuse by the same account,
  -- another logical slot, or another campaign day is refused.
  if exists (
    select 1 from public.content_calendar c
     where c.gym_id = 'lasso'
       and lower(btrim(coalesce(c.format, 'feed'))) = 'feed'
       and c.variant_status = 'active'
       and c.status in ('pending', 'approved', 'publishing', 'published', 'draft')
       and c.post_date between date '2026-09-23' and date '2026-11-08'
       and (c.image_url = v_image_url or c.caption = v_caption)
       and not (
         c.post_date = v_day
         and c.slot_index = v_slot
         and lower(btrim(coalesce(c.account, ''))) in ('instagram', 'facebook')
         and lower(btrim(coalesce(c.account, ''))) <> v_account
       )
  ) then
    return jsonb_build_object('result', 'conflict', 'reason', 'cross_day_creative_reuse');
  end if;

  insert into public.content_calendar (
    id, gym_id, account, post_date, pillar, format, caption, image_url,
    status, scheduled_at, slot_index, variant_status, visual_group_key, byte_hash
  ) values (
    v_id, 'lasso', v_account, v_day, p_row->>'pillar', 'feed', v_caption,
    v_image_url, 'pending', v_scheduled_at,
    v_slot, 'active', p_visual_group_key, p_byte_hash
  );

  return jsonb_build_object('result', 'inserted', 'id', v_id);
exception
  when unique_violation then
    -- Defensive only: the table lock makes an ordinary concurrent insert wait.
    return jsonb_build_object('result', 'conflict', 'reason', 'unique_violation');
end;
$$;

revoke all on function public.stage_lasso_campaign_row(jsonb, text, text, text, text, text, text)
  from public, anon, authenticated;
grant execute on function public.stage_lasso_campaign_row(jsonb, text, text, text, text, text, text)
  to service_role;
