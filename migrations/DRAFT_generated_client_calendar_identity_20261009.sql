-- DRAFT / UNAPPLIED. Install before portal 0625, whose row trigger compares
-- old/new content_calendar.byte_hash during generated creative updates.
-- This compatibility prerequisite adds no identity or trust semantics.
begin;
do $$
declare
  v_column record;
begin
  if to_regclass('public.content_calendar') is null then
    raise exception 'calendar byte identity requires public.content_calendar'
      using errcode = '23514';
  end if;

  select a.atttypid, a.atttypmod, a.attnotnull, a.attgenerated, a.attidentity,
         d.oid as default_oid
    into v_column
  from pg_attribute a
  left join pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum
  where a.attrelid = 'public.content_calendar'::regclass
    and a.attname = 'byte_hash'
    and a.attnum > 0
    and not a.attisdropped;

  if not found then
    alter table public.content_calendar add column byte_hash text;
  elsif v_column.atttypid <> 'text'::regtype
     or v_column.atttypmod <> -1
     or v_column.attnotnull
     or v_column.attgenerated <> ''
     or v_column.attidentity <> ''
     or v_column.default_oid is not null then
    raise exception 'content_calendar.byte_hash exists with an incompatible shape'
      using errcode = '23514';
  end if;
end
$$;
commit;
