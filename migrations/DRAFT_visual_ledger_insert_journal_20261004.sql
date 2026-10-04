-- DRAFT: Visual ledger INSERT observation journal (2026-10-04)
--
-- Purpose: immutable, append-only OBSERVATION of visual_group_usage_ledger
-- INSERT events. This is an observation journal ONLY. It is NOT evidence of
-- original use, NOT provider-byte clearance, and changes no coverage
-- evaluator, activation function, import interpretation or writer. Existing
-- fingerprint-free history stays unresolved exactly as before; nothing here
-- reads xmin, clock time or current media as proof of original use.
--
-- Semantics:
--   * visual_group_usage_ledger gains nullable observed_insert_event_id.
--     NO backfill: rows that exist before this draft keep NULL forever.
--   * BEFORE INSERT assigns a fresh server-generated UUID. A caller-supplied
--     value is discarded; the ID can never be caller-selected or reused.
--   * AFTER INSERT records, in the SAME transaction, the exact raw ledger
--     primary key, reserved_date, raw calendar_row_id, channel, state and an
--     immutable JSONB snapshot of the row exactly as inserted.
--   * The journal row has NO foreign key to content_calendar (survives
--     calendar deletion) and no foreign key to the ledger (survives ledger
--     deletion and avoids circular insertion). The ONE FK is ledger ->
--     journal, DEFERRABLE INITIALLY DEFERRED, validated at commit after the
--     AFTER trigger has written the journal row: no circular insert failure.
--   * UPDATEs of existing ledger rows create NO event and can NEVER change
--     observed_insert_event_id. Delete + reinsert creates a NEW event.
--   * Callers, including service_role, cannot mutate the journal directly.
--     Journal writes happen only through the SECURITY DEFINER trigger
--     functions owned by the migration role; RLS + revoked grants block
--     every direct path, and an explicit trigger rejects UPDATE/DELETE/
--     TRUNCATE as defense in depth (the table owner superuser path remains,
--     as with any PostgreSQL table).
--   * The trigger functions themselves are locked down: EXECUTE is revoked
--     from PUBLIC/anon/authenticated/service_role (so no caller can invoke
--     or attach them), and each function verifies TG_RELID/TG_OP/TG_WHEN/
--     TG_LEVEL, so a same-shaped TEMP table can never fire the recorder to
--     forge a journal row.
--
-- This file is a DRAFT. Do not apply to production without owner sign-off.

begin;

-- 0. Preconditions -----------------------------------------------------------

do $$
begin
  if not exists (
    select 1 from information_schema.tables
    where table_schema = 'public'
      and table_name = 'visual_group_usage_ledger'
  ) then
    raise exception
      'visual_group_usage_ledger must exist before installing the insert journal';
  end if;
end $$;

-- 1. Nullable observation column. No backfill, deliberately. -----------------

alter table public.visual_group_usage_ledger
  add column if not exists observed_insert_event_id uuid;

comment on column public.visual_group_usage_ledger.observed_insert_event_id is
  'Server-assigned UUID of this row''s INSERT observation event in '
  'visual_group_insert_journal. NULL for every row that predates the journal '
  '(never backfilled). Immutable once set; updates create no event.';

-- 2. Append-only journal -----------------------------------------------------

create table if not exists public.visual_group_insert_journal (
  event_id        uuid        primary key,
  ledger_gym_id   text        not null,
  ledger_group_key text       not null,
  reserved_date   date,
  calendar_row_id uuid,       -- raw value as inserted; NO FK to content_calendar
  channel         text,
  state           text        not null,
  insert_snapshot jsonb       not null,
  recorded_at     timestamptz not null default now()
);

comment on table public.visual_group_insert_journal is
  'Append-only observation of visual_group_usage_ledger INSERT events. Raw '
  'keys and values are preserved verbatim; survives ledger and calendar '
  'deletion. Observation only: NOT original-use evidence, NOT provider-byte '
  'clearance, and feeds no coverage/activation/import logic.';

create index if not exists visual_group_insert_journal_ledger_idx
  on public.visual_group_insert_journal (ledger_gym_id, ledger_group_key, recorded_at);

-- 3. Trigger functions -------------------------------------------------------
-- SECURITY DEFINER: journal writes bypass the revoked caller grants and RLS
-- through the function owner (the migration role), never through caller
-- privileges. search_path is pinned to public.

create or replace function public.visual_group_ledger_assign_insert_event()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  -- Anti-forgery binding: this function may run ONLY as the BEFORE INSERT
  -- row trigger on the real ledger. Revoked EXECUTE (below) already stops a
  -- caller attaching it to a same-shaped TEMP table; this guard rejects any
  -- residual path so a forged source can never mint an event ID.
  if tg_relid is distinct from 'public.visual_group_usage_ledger'::regclass
     or tg_op is distinct from 'INSERT'
     or tg_when is distinct from 'BEFORE'
     or tg_level is distinct from 'ROW' then
    raise exception
      'visual_group_ledger_assign_insert_event is bound to BEFORE INSERT ROW on public.visual_group_usage_ledger only';
  end if;
  -- Always a fresh server-generated UUID. Any caller-supplied value in
  -- NEW.observed_insert_event_id is discarded, so the event ID can never be
  -- caller-selected or reused across rows.
  NEW.observed_insert_event_id := gen_random_uuid();
  return NEW;
end $$;

create or replace function public.visual_group_ledger_record_insert()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  -- Anti-forgery binding: ONLY the real AFTER INSERT row trigger on the
  -- ledger may write journal rows. A caller-controlled TEMP (or other) table
  -- can never use this recorder to forge an observation.
  if tg_relid is distinct from 'public.visual_group_usage_ledger'::regclass
     or tg_op is distinct from 'INSERT'
     or tg_when is distinct from 'AFTER'
     or tg_level is distinct from 'ROW' then
    raise exception
      'visual_group_ledger_record_insert is bound to AFTER INSERT ROW on public.visual_group_usage_ledger only';
  end if;
  insert into public.visual_group_insert_journal
    (event_id, ledger_gym_id, ledger_group_key, reserved_date,
     calendar_row_id, channel, state, insert_snapshot)
  values
    (NEW.observed_insert_event_id, NEW.gym_id, NEW.group_key,
     NEW.reserved_date, NEW.calendar_row_id, NEW.channel, NEW.state,
     to_jsonb(NEW) - 'observed_insert_event_id');
  return null;
end $$;

create or replace function public.visual_group_ledger_event_id_immutable()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if tg_relid is distinct from 'public.visual_group_usage_ledger'::regclass
     or tg_op is distinct from 'UPDATE'
     or tg_when is distinct from 'BEFORE'
     or tg_level is distinct from 'ROW' then
    raise exception
      'visual_group_ledger_event_id_immutable is bound to BEFORE UPDATE ROW on public.visual_group_usage_ledger only';
  end if;
  if NEW.observed_insert_event_id is distinct from OLD.observed_insert_event_id then
    raise exception
      'observed_insert_event_id is immutable: updates create no insert event '
      'and cannot assign, change or clear one (gym_id=%, group_key=%)',
      OLD.gym_id, OLD.group_key;
  end if;
  return NEW;
end $$;

-- 3b. Trigger functions are never caller-executable -------------------------
-- CREATE FUNCTION grants EXECUTE to PUBLIC by default. Revoke it (and any
-- role grant) on all four journal functions: without EXECUTE no caller can
-- invoke them directly OR attach them to a forged table. Trigger firing does
-- not check EXECUTE, so the real ledger triggers are unaffected.

revoke all on function public.visual_group_ledger_assign_insert_event()
  from public, anon, authenticated, service_role;
revoke all on function public.visual_group_ledger_record_insert()
  from public, anon, authenticated, service_role;
revoke all on function public.visual_group_ledger_event_id_immutable()
  from public, anon, authenticated, service_role;

-- 4. Ledger triggers ---------------------------------------------------------

drop trigger if exists visual_group_ledger_assign_insert_event
  on public.visual_group_usage_ledger;
create trigger visual_group_ledger_assign_insert_event
  before insert on public.visual_group_usage_ledger
  for each row execute function public.visual_group_ledger_assign_insert_event();

drop trigger if exists visual_group_ledger_record_insert
  on public.visual_group_usage_ledger;
create trigger visual_group_ledger_record_insert
  after insert on public.visual_group_usage_ledger
  for each row execute function public.visual_group_ledger_record_insert();

drop trigger if exists visual_group_ledger_event_id_immutable
  on public.visual_group_usage_ledger;
create trigger visual_group_ledger_event_id_immutable
  before update on public.visual_group_usage_ledger
  for each row execute function public.visual_group_ledger_event_id_immutable();

-- 5. Bind ledger -> journal. Deferred so the AFTER-trigger journal write in
--    the same statement satisfies the FK at commit time: no circular
--    insertion failure. Journal itself references nothing mutable.

alter table public.visual_group_usage_ledger
  drop constraint if exists visual_group_ledger_insert_event_fkey;
alter table public.visual_group_usage_ledger
  add constraint visual_group_ledger_insert_event_fkey
  foreign key (observed_insert_event_id)
  references public.visual_group_insert_journal (event_id)
  deferrable initially deferred;

-- 6. Journal is append-only --------------------------------------------------

create or replace function public.visual_group_insert_journal_immutable()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if tg_relid is distinct from 'public.visual_group_insert_journal'::regclass
     or tg_op not in ('UPDATE', 'DELETE', 'TRUNCATE') then
    raise exception
      'visual_group_insert_journal_immutable is bound to UPDATE/DELETE/TRUNCATE on public.visual_group_insert_journal only';
  end if;
  raise exception
    'visual_group_insert_journal is append-only: update/delete/truncate rejected';
end $$;

-- Lockdown (moved after creation: the function must exist to revoke on it).
revoke all on function public.visual_group_insert_journal_immutable()
  from public, anon, authenticated, service_role;

drop trigger if exists visual_group_insert_journal_immutable_row
  on public.visual_group_insert_journal;
create trigger visual_group_insert_journal_immutable_row
  before update or delete on public.visual_group_insert_journal
  for each row execute function public.visual_group_insert_journal_immutable();

drop trigger if exists visual_group_insert_journal_immutable_stmt
  on public.visual_group_insert_journal;
create trigger visual_group_insert_journal_immutable_stmt
  before truncate on public.visual_group_insert_journal
  for each statement execute function public.visual_group_insert_journal_immutable();

-- 7. Caller lockout ----------------------------------------------------------
-- Nobody calls the journal directly. service_role may READ; no caller may
-- INSERT/UPDATE/DELETE/TRUNCATE. Trigger functions write via their owner.

alter table public.visual_group_insert_journal enable row level security;

revoke all on public.visual_group_insert_journal
  from public, anon, authenticated, service_role;
grant select on public.visual_group_insert_journal to service_role;

drop policy if exists visual_group_insert_journal_service_read
  on public.visual_group_insert_journal;
create policy visual_group_insert_journal_service_read
  on public.visual_group_insert_journal
  for select to service_role using (true);

commit;
