-- DRAFT / UNAPPLIED / DEFAULT OFF. No production activation is authorized.
-- Forward lock entry, publish/caption tranche (child 0, publish-caption lane):
-- the three production functions below acquire the ordinary entry sequence
-- FIRST -- the graph shared lock 'G' (pg_advisory_xact_lock_shared on
-- 'fixer_forward_graph_20261006'), then the census-exclusive transaction entry
-- lock 'C' (pg_advisory_xact_lock on 'fixer_forward_photo_census_20261007') --
-- before ANY existing advisory, row or table lock they already take, by calling
-- public.fixer_forward_calendar_entry_lock_20261008() as the very first
-- statement of each body. This matches the entry order and helper contract
-- established by DRAFT_fixer_forward_lock_entry_calendar_20261008.sql: no
-- caller may hold a row lock or tenant advisory lock while waiting on G or C,
-- so a graph/census wait can never invert the lock order. Runtime publishers
-- never upgrade the shared graph lock; attestation graph mutation is the only
-- exclusive graph holder and it waits without holding calendar rows.
--
-- Both advisory keys are the EXISTING system-wide keys, not new tranche-local
-- keys: the ordering only protects if every participant serializes on the same
-- lock namespace.
--
-- Helper (prerequisite, NOT created here):
--   public.fixer_forward_calendar_entry_lock_20261008()
--   returns void, language plpgsql, security definer,
--   set search_path=pg_catalog,public. It raises (errcode 25000) unless the
--   transaction isolation is read committed, then takes G (shared) then C
--   (exclusive), in that order. EXECUTE is revoked from
--   public/anon/authenticated/service_role by its own draft migration: the
--   replaced functions here are SECURITY DEFINER and reach it as their owner.
--   This migration FAILS CLOSED (errcode 42883) unless the helper already
--   exists with that exact shape, before touching anything else.
--
-- Function text: each CREATE OR REPLACE below is the exact frozen production
-- definition from evidence/portal-function-definitions-20261008.sql with ONLY
-- the entry-lock call inserted at the top of the body. Signatures, defaults,
-- SECURITY DEFINER, search_path, return contracts, tenant/approval/idempotency
-- checks and capacity rules are unchanged. No provider (external API) call
-- exists in these bodies; nothing is added outside DB lock scope.
--
-- WRAPPER NO-OP EVIDENCE (inspected, deliberately NOT redefined here):
--   * public.claim_calendar_gbp_publish_with_mode_owned(p_row_id uuid,
--     p_gym_id text) returns jsonb -- frozen prosrc:
--       declare v_row public.content_calendar%rowtype; begin
--         select * into v_row from public.claim_calendar_gbp_publish_owned(
--           p_row_id, p_gym_id);
--         if not found then return null; end if;
--         return jsonb_build_object('row', to_jsonb(v_row),
--           'autonomous_at_claim', public.calendar_gym_is_autonomous(p_gym_id));
--       end;
--     It takes NO advisory lock, NO row lock and NO table lock before
--     delegating (frozen inventory has_for_update=false,
--     has_advisory_lock=false). Its only pre-delegation statement is the
--     SELECT ... INTO from the callee, which itself (redefined in
--     DRAFT_fixer_forward_lock_entry_calendar_20261008.sql) acquires the entry
--     sequence first. Redefining the wrapper would add nothing but noise.
--   * public.claim_calendar_publish_slot_proven_owned(p_row_id uuid,
--     p_gym_id text, p_day date, p_timezone text, p_capacity integer,
--     p_approved_only boolean) returns jsonb -- frozen prosrc:
--       declare v_token uuid; begin
--         v_token := public.claim_calendar_publish_slot_owned(
--           p_row_id, p_gym_id, p_day, p_timezone, p_capacity,
--           p_approved_only, true);
--         if v_token is null then return null; end if;
--         return (select jsonb_build_object('row', to_jsonb(c),
--             'autonomous_at_claim', public.calendar_gym_is_autonomous(p_gym_id))
--           from public.content_calendar c
--           where c.id = p_row_id and c.gym_id = p_gym_id
--             and c.status = 'publishing' and c.publish_claim_token = v_token);
--       end;
--     Same reasoning: no pre-delegation lock of any kind (inventory
--     has_for_update=false, has_advisory_lock=false); the callee
--     claim_calendar_publish_slot_owned takes the entry sequence first under
--     the sibling draft. The trailing jsonb SELECT is a plain read after the
--     claim already holds its locks. Both wrappers are therefore guarded in
--     the drift check below (so a future edit that adds a lock to a wrapper
--     aborts this migration) but are NOT redefined.
--
-- Drift precondition: the DO block below aborts the whole migration unless
-- every target function still exists exactly once in public with
-- exact identity arguments and postgres owner from the read-only
-- evidence/portal-entry-function-identities-p2b-20261008.json,
-- applying current_user postgres, and md5(prosrc) equal to the frozen inventory hash
-- (evidence/portal-legacy-function-inventory-20261008.json, query:
-- md5(p.prosrc)). Any production drift -- body edit, drop, or a duplicate
-- overload -- raises 23514 and rolls the transaction back before any replace.
--
-- Rollback before use: restore the frozen definitions from the evidence file.
begin;

-- PRECONDITION GUARD: helper prerequisite, then frozen production definitions
-- (abort on any drift).
do $$
declare
  v_name text;
  v_expected text;
  v_count integer;
  v_actual text;
  v_identity text;
  v_expected_identity text;
  v_owner text;
  v_helper_valid boolean;
begin
  if current_user is distinct from 'postgres' then
    raise exception 'forward lock entry DRAFT: current_user must be postgres' using errcode = '23514';
  end if;
  -- Pin the accepted P2a helper body: read-committed isolation guard,
  -- G shared then C exclusive, no runtime graph upgrade. Exact prosrc hash
  -- also rejects no-op helpers or altered advisory namespaces/order.
  select count(*), bool_and(
           p.pronargs = 0
           and pg_get_function_identity_arguments(p.oid) = ''
           and p.prorettype = 'void'::regtype
           and pg_get_userbyid(p.proowner) = 'postgres'
           and p.prosecdef
           and l.lanname = 'plpgsql'
           and p.proconfig = array['search_path=pg_catalog, public']::text[]
           and md5(p.prosrc) = 'f7804f90164613bff2e70d7f7bc5b4e2')
    into v_count, v_helper_valid
    from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    join pg_language l on l.oid = p.prolang
   where n.nspname = 'public'
     and p.proname = 'fixer_forward_calendar_entry_lock_20261008';
  if v_count is distinct from 1 or v_helper_valid is distinct from true then
    raise exception 'forward lock entry DRAFT: prerequisite helper public.fixer_forward_calendar_entry_lock_20261008() is missing or has the wrong shape (definitions: %); apply the accepted helper draft first',
      v_count using errcode = '42883';
  end if;

  -- Frozen prosrc drift guard: redefined functions AND the two inspected
  -- no-op wrappers must all match the frozen 2026-10-08 inventory exactly.
  for v_name, v_expected, v_expected_identity in
    select f.proname, f.body_md5, f.identity_args from (values
      ('calendar_patch_caption_autonomous_clean', '52fac209620734e6ba3a3c4a53c0f6a7', 'p_row_id uuid, p_gym_id text, p_expected_status text, p_expected_caption text, p_clean_caption text'),
      ('calendar_patch_caption_manual_format', '72534b54f79b5ae419562963dbf55ba0', 'p_row_id uuid, p_gym_id text, p_expected_status text, p_expected_caption text, p_clean_caption text'),
      ('claim_calendar_publish_slot', '166a4f2df64047a0449ec708027f861d', 'p_row_id uuid, p_gym_id text, p_day date, p_timezone text, p_capacity integer, p_approved_only boolean'),
      ('claim_calendar_gbp_publish_with_mode_owned', '528dcc3c13a98f705cf3c021d0a60dae', 'p_row_id uuid, p_gym_id text'),
      ('claim_calendar_publish_slot_proven_owned', 'c36ca7a901a9a84c772dad34c1c439b2', 'p_row_id uuid, p_gym_id text, p_day date, p_timezone text, p_capacity integer, p_approved_only boolean')
    ) as f(proname, body_md5, identity_args)
  loop
    select count(*), max(md5(p.prosrc)),
           max(pg_get_function_identity_arguments(p.oid)), max(pg_get_userbyid(p.proowner))
      into v_count, v_actual, v_identity, v_owner
      from pg_proc p join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public' and p.proname = v_name;
    if v_count is distinct from 1 or v_actual is distinct from v_expected
        or v_identity is distinct from v_expected_identity
        or v_owner is distinct from 'postgres' then
      raise exception 'forward lock entry DRAFT: production definition of % drifted from the frozen 2026-10-08 inventory (count %, md5 %, identity %, owner %); aborting before any replace',
        v_name, v_count, v_actual, v_identity, v_owner using errcode = '23514';
    end if;
  end loop;
end $$;

CREATE OR REPLACE FUNCTION public.calendar_patch_caption_autonomous_clean(p_row_id uuid, p_gym_id text, p_expected_status text, p_expected_caption text, p_clean_caption text)
 RETURNS SETOF content_calendar
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
begin
  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census
  -- exclusive 'C', before the tenant advisory lock below.
  perform public.fixer_forward_calendar_entry_lock_20261008();
  if p_expected_status not in ('pending', 'approved')
      or p_clean_caption is null or p_clean_caption = p_expected_caption then
    return;
  end if;
  perform pg_advisory_xact_lock(hashtextextended(p_gym_id, 0));
  if not public.calendar_gym_is_autonomous(p_gym_id) then
    return;
  end if;
  return query update public.content_calendar c
    set caption = p_clean_caption, approval_kind = null, approved_by = null,
        approved_at = null, approval_digest = null
    where c.id = p_row_id and c.gym_id = p_gym_id
      and c.status = p_expected_status
      and c.caption is not distinct from p_expected_caption
      and c.variant_status = 'active' and c.published_at is null
      and c.late_post_id is null and c.publish_claim_token is null
    returning c.*;
end;
$function$
;

CREATE OR REPLACE FUNCTION public.calendar_patch_caption_manual_format(p_row_id uuid, p_gym_id text, p_expected_status text, p_expected_caption text, p_clean_caption text)
 RETURNS SETOF content_calendar
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_gym_count integer;
  v_gym_uuid uuid;
  v_settings_count integer;
  v_autonomous boolean;
begin
  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census
  -- exclusive 'C', before the tenant advisory lock and the FOR SHARE row
  -- locks below.
  perform public.fixer_forward_calendar_entry_lock_20261008();
  if p_row_id is null or nullif(btrim(coalesce(p_gym_id, '')), '') is null
      or p_expected_status not in ('pending', 'approved')
      or p_clean_caption is null
      or p_clean_caption is not distinct from p_expected_caption then
    return;
  end if;
  perform pg_advisory_xact_lock(hashtextextended(p_gym_id, 0));
  select count(*), min(g.id::text)::uuid into v_gym_count, v_gym_uuid
    from public.gyms g
    join public.echo_intake_tokens t on t.gym_id = g.id
   where t.echo_account_key = p_gym_id
     and lower(coalesce(g.slug, '')) not like '%archived%'
     and lower(coalesce(g.slug, '')) not like '%-dup%'
     and lower(coalesce(g.name, '')) not like '%archived%'
     and lower(coalesce(g.name, '')) not like '%do not use%';
  if v_gym_count is distinct from 1 then return; end if;
  perform 1 from public.echo_intake_tokens t
   where t.gym_id = v_gym_uuid and t.echo_account_key = p_gym_id
   for share;
  if not found then return; end if;
  select count(*) into v_settings_count from public.echo_gym_settings s
   where s.gym_id = v_gym_uuid;
  if v_settings_count is distinct from 1 then return; end if;
  select s.autonomous into v_autonomous from public.echo_gym_settings s
   where s.gym_id = v_gym_uuid for share;
  if v_autonomous is distinct from false then return; end if;

  -- UPDATE is the row lock and CAS. Any claim, approval or caption edit that
  -- wins first changes these predicates. A changed Manual approval must be
  -- reviewed again, including rows previously approved in Automatic mode.
  return query update public.content_calendar c
     set caption = p_clean_caption, status = 'pending',
         approval_kind = null, approved_by = null,
         approved_at = null, approval_digest = null
   where c.id = p_row_id and c.gym_id = p_gym_id
     and c.status = p_expected_status
     and c.caption is not distinct from p_expected_caption
     and c.variant_status = 'active' and c.published_at is null
     and c.late_post_id is null and c.publish_claim_token is null
     and c.publish_reservation_day is null
   returning c.*;
end;
$function$
;

CREATE OR REPLACE FUNCTION public.claim_calendar_publish_slot(p_row_id uuid, p_gym_id text, p_day date, p_timezone text, p_capacity integer, p_approved_only boolean)
 RETURNS boolean
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_row public.content_calendar%rowtype;
  v_used integer;
begin
  -- FORWARD LOCK ENTRY (2026-10-08 DRAFT): graph shared 'G', then census
  -- exclusive 'C', before the tenant advisory lock and the FOR UPDATE row
  -- lock below.
  perform public.fixer_forward_calendar_entry_lock_20261008();
  if p_capacity not between 1 and 2 or p_day is null or p_timezone is null then
    return false;
  end if;
  -- Lock across day boundaries too: a previous-day in-flight claim must be
  -- visible to a worker reserving the next local day at midnight.
  perform pg_advisory_xact_lock(hashtextextended(p_gym_id, 0));
  select * into v_row from public.content_calendar
    where id = p_row_id and gym_id = p_gym_id
      and status in ('pending', 'approved') and published_at is null
      and late_post_id is null
      and variant_status = 'active'
    for update;
  if not found or (p_approved_only and v_row.status <> 'approved') then
    return false;
  end if;

  select count(*) into v_used from public.content_calendar
    where gym_id = p_gym_id
      and lower(btrim(coalesce(account, ''))) =
          lower(btrim(coalesce(v_row.account, '')))
      and coalesce(nullif(lower(btrim(format)), ''), 'feed') =
          coalesce(nullif(lower(btrim(v_row.format)), ''), 'feed')
      and (status = 'publishing' -- legacy and previous-day in-flight claims block
           or (status = 'published' and
               (publish_reservation_day = p_day
                or (published_at is not null and
                    (published_at at time zone p_timezone)::date = p_day))));
  if v_used >= p_capacity then
    return false;
  end if;
  update public.content_calendar
    set status = 'publishing', publish_reservation_day = p_day
    where id = p_row_id;
  return true;
end;
$function$
;

commit;
