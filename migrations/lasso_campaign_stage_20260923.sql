-- Atomic, insert-only staging for the dated LASSO campaign refresh.
--
-- This RPC deliberately takes a SHARE ROW EXCLUSIVE lock on content_calendar.
-- The lock conflicts with INSERT/UPDATE/DELETE's ROW EXCLUSIVE lock, so the
-- logical-slot and media-reuse checks cannot race even with writers that do not
-- know about this function.  The transaction is intentionally short: validate,
-- lock, verify one artifact row, inspect calendar rows, insert one row, return.
-- Apply manually only after review; this migration does not stage any content.

create or replace function public.stage_lasso_campaign_row(
  p_row jsonb,
  p_artifact_tenant text,
  p_source_hash text,
  p_policy_version text,
  p_image_sha256 text
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
begin
  if p_row is null or jsonb_typeof(p_row) <> 'object' then
    return jsonb_build_object('result', 'conflict', 'reason', 'invalid_row');
  end if;

  -- Reject invented/unknown provenance and every calendar field this narrow
  -- staging path is not allowed to write.  In particular, the production
  -- calendar schema has no category or draft_type column.
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

  -- Serialize against every ordinary content_calendar writer, not only callers
  -- that voluntarily take the campaign advisory lock.
  lock table public.content_calendar in share row exclusive mode;

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

  select * into v_existing
    from public.content_calendar
   where id = v_id;
  if found then
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
       and v_existing.scheduled_at is not distinct from v_scheduled_at then
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
    status, scheduled_at, slot_index, variant_status
  ) values (
    v_id, 'lasso', v_account, v_day, p_row->>'pillar', 'feed', v_caption,
    v_image_url, 'pending', v_scheduled_at,
    v_slot, 'active'
  );

  return jsonb_build_object('result', 'inserted', 'id', v_id);
exception
  when unique_violation then
    -- Defensive only: the table lock makes an ordinary concurrent insert wait.
    return jsonb_build_object('result', 'conflict', 'reason', 'unique_violation');
end;
$$;

revoke all on function public.stage_lasso_campaign_row(jsonb, text, text, text, text)
  from public, anon, authenticated;
grant execute on function public.stage_lasso_campaign_row(jsonb, text, text, text, text)
  to service_role;
