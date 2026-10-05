-- DRAFT/OFF. Review and apply separately before ECHO_ATOMIC_MONTH_REPLACE_DRAFT=1.
-- One call is one PostgreSQL transaction. Any validation, DELETE, INSERT, or trigger
-- failure rolls the whole replacement back. The client supplies a frozen active-row
-- snapshot; a changed approval/hold/row set fails closed before DELETE.
create or replace function public.echo_replace_calendar_months_atomic_draft(
    p_gym_id text,
    p_months text[],
    p_expected jsonb,
    p_rows jsonb,
    p_preserve_dates text[] default '{}',
    p_protected_story_ids text[] default '{}'
) returns jsonb
language plpgsql
set search_path = public, pg_temp
set lock_timeout = '5s'
set statement_timeout = '45s'
as $$
declare
    month_text text;
    row_value jsonb;
    row_key text;
    actual jsonb;
    inserted_rows jsonb;
    col_names text;
    deleted_count integer;
begin
    if p_gym_id is null or btrim(p_gym_id) = '' or p_months is null
       or cardinality(p_months) = 0 or p_expected is null
       or jsonb_typeof(p_expected) <> 'array' or p_rows is null
       or jsonb_typeof(p_rows) <> 'array' or jsonb_array_length(p_rows) = 0 then
        raise exception 'invalid atomic month request' using errcode = '22023';
    end if;
    if cardinality(p_months) <> (select count(distinct m) from unnest(p_months) m) then
        raise exception 'duplicate month scope' using errcode = '22023';
    end if;
    foreach month_text in array p_months loop
        if month_text !~ '^[0-9]{4}-(0[1-9]|1[0-2])$' then
            raise exception 'invalid month scope' using errcode = '22023';
        end if;
    end loop;

    -- Ordinary portal PATCH/INSERT paths do not take our tenant advisory lock.
    -- Hold a short table write lock so no phantom insert or approval can slip
    -- between the CAS read and DELETE/INSERT. Review lock impact before rollout.
    lock table public.content_calendar in share row exclusive mode;
    perform pg_advisory_xact_lock(hashtextextended(p_gym_id, 41723));
    perform 1 from public.content_calendar c
      where c.gym_id = p_gym_id and left(c.post_date::text, 7) = any(p_months)
        and c.variant_status = 'active' for update;
    select coalesce(jsonb_agg(jsonb_build_object(
        'id', c.id::text, 'status', c.status,
        'variant_status', c.variant_status,
        'media_not_ready_reason', c.media_not_ready_reason,
        'post_date', c.post_date::text, 'account', c.account,
        'format', c.format, 'caption', c.caption,
        'image_url', c.image_url, 'thumbnail_url', c.thumbnail_url,
        'source_media_url', c.source_media_url) order by c.id::text), '[]'::jsonb)
      into actual
      from public.content_calendar c
      where c.gym_id = p_gym_id and left(c.post_date::text, 7) = any(p_months)
        and c.variant_status = 'active';
    if actual <> p_expected then
        raise exception 'calendar month changed since preflight' using errcode = '40001';
    end if;

    for row_value in select value from jsonb_array_elements(p_rows) loop
        if jsonb_typeof(row_value) <> 'object'
           or row_value->>'gym_id' is distinct from p_gym_id
           or not (left(coalesce(row_value->>'post_date', ''), 7)
                   = any(p_months)) then
            raise exception 'row outside tenant or month scope' using errcode = '22023';
        end if;
        if row_value ? 'id' then
            raise exception 'caller may not supply calendar id' using errcode = '22023';
        end if;
        if coalesce(row_value->>'status', '') not in
           ('pending', 'approved', 'denied') then
            raise exception 'unsupported staged status' using errcode = '22023';
        end if;
    end loop;
    for row_key in select distinct k.key from jsonb_array_elements(p_rows) r(value),
                   lateral jsonb_object_keys(r.value) k(key) loop
        if row_key not in (
            'gym_id', 'account', 'post_date', 'pillar', 'format', 'caption',
            'image_url', 'status', 'thumbnail_url', 'source_media_url',
            'source_media_asset_id', 'source_media_content_hash', 'slot_index',
            'event_id', 'media_not_ready_reason', 'logical_post_id',
            'hook_family', 'ask_type', 'caption_len_band', 'time_slot',
            'has_member_face', 'people', 'drive_file_id', 'r2_key',
            'visual_group_key', 'byte_hash'
        ) or not exists (select 1 from pg_attribute a
                       where a.attrelid = 'public.content_calendar'::regclass
                         and a.attname = row_key and a.attnum > 0
                         and not a.attisdropped and a.attgenerated = '') then
            raise exception 'unsupported calendar column' using errcode = '22023';
        end if;
    end loop;

    delete from public.content_calendar c
      where c.gym_id = p_gym_id and left(c.post_date::text, 7) = any(p_months)
        and c.variant_status = 'active'
        and (c.status is null or c.status in ('pending', 'draft', 'queued'))
        and c.media_not_ready_reason is null
        and c.post_date::text <> all(coalesce(p_preserve_dates, '{}'))
        and not (c.format = 'story' and c.id::text = any(
            coalesce(p_protected_story_ids, '{}')));
    get diagnostics deleted_count = row_count;

    -- A retained approval or media hold still owns its exact slot. Refuse the
    -- whole transaction if a proposal collides; the preceding DELETE rolls back.
    if exists (
        select 1 from jsonb_to_recordset(p_rows) as x(
            post_date date, account text, format text, time_slot text,
            slot_index integer)
        join public.content_calendar c on c.gym_id = p_gym_id
          and c.variant_status = 'active' and c.post_date = x.post_date
          and c.account is not distinct from x.account
          and c.format is not distinct from x.format
          and c.time_slot is not distinct from x.time_slot
          and c.slot_index is not distinct from x.slot_index
    ) then
        raise exception 'retained calendar slot collision' using errcode = '23514';
    end if;
    if exists (
        select 1 from jsonb_to_recordset(p_rows) as x(
            post_date date, account text, format text, time_slot text,
            slot_index integer)
        group by x.post_date, x.account, x.format, x.time_slot, x.slot_index
        having count(*) > 1
    ) then
        raise exception 'duplicate atomic month slot' using errcode = '23514';
    end if;

    select string_agg(format('%I', key), ', ' order by key)
      into col_names
      from (select distinct k.key from jsonb_array_elements(p_rows) r(value),
                   lateral jsonb_object_keys(r.value) k(key)) names;
    execute format(
       'with inserted as (insert into public.content_calendar (%1$s) '
       || 'select %1$s from jsonb_populate_recordset('
       || 'null::public.content_calendar, $1) x returning *) '
       || 'select coalesce(jsonb_agg(to_jsonb(inserted)), ''[]''::jsonb) '
       || 'from inserted', col_names)
      using p_rows into inserted_rows;
    if jsonb_array_length(inserted_rows) <> jsonb_array_length(p_rows) then
        raise exception 'atomic insert count mismatch' using errcode = '23514';
    end if;
    return jsonb_build_object('deleted', deleted_count,
                              'inserted', jsonb_array_length(inserted_rows),
                              'rows', inserted_rows);
end;
$$;

revoke all on function public.echo_replace_calendar_months_atomic_draft(
    text, text[], jsonb, jsonb, text[], text[]) from public;
grant execute on function public.echo_replace_calendar_months_atomic_draft(
    text, text[], jsonb, jsonb, text[], text[]) to service_role;
