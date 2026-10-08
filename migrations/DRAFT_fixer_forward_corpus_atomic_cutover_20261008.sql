-- DRAFT / UNAPPLIED / DEFAULT OFF. No production DDL is authorized.
-- Apply AFTER B's calendar/media/LASSO entry migrations and owner-photo stack.
-- This is ONE atomic cutover: ordinary statement guards and removal of ONLY
-- the two frozen content_calendar SHARE ROW EXCLUSIVE clauses commit together.
-- Never apply individual sections or install the blocking trigger beforehand.
-- C now excludes every ordinary INSERT/UPDATE/DELETE in the guarded corpus;
-- removing SRE avoids relation -> C versus C -> SRE deadlocks. Volatile RPCs
-- re-read at Read Committed after waiting on C; business predicates are intact.
-- Owner/attester entry already takes its FINAL G exclusive mode before C.
-- Ordinary DML takes G shared -> C. Negative authority tables take FINAL G
-- exclusive -> C (never shared -> exclusive). Statement triggers run after
-- implicit RowExclusive relation acquisition, but before row locks.
-- Lock and statement deadlines belong to the caller; the trigger does not
-- replace a caller's lock_timeout with a shorter function-local deadline.
--
-- The numeric trigger name sorts before owner_photo_corpus_write and the
-- optional #345 generated_inventory_lock. Their existing try-lock checks
-- therefore encounter transaction-owned G/C and succeed reentrantly. Preserve
-- those triggers and their TRUNCATE/authority checks. The optional generated
-- trigger also sorts later when #345 is installed AFTER this B migration.
-- Revoke tenant TRUNCATE explicitly: it otherwise takes AccessExclusive before
-- a statement trigger and can form the same relation/C inversion. No new
-- privileges, approval, tenant, sender, provider or release activation.
--
-- Frozen input hashes are the actual 2026-10-08 B entry draft prosrc bodies,
-- NOT the original production bodies. Identity/owner/security/helper hashes
-- and trigger-order checks fail closed. pg_get_functiondef preserves function
-- options; CREATE OR REPLACE preserves OID, owner and ACL. Each resulting body
-- must equal its frozen input with precisely ONE lock statement line removed.
-- Rollback before use: restore BOTH frozen B definitions and drop all new
-- statement triggers/function in ONE transaction. Do not restore tenant
-- TRUNCATE privileges while any graph/census protocol remains in use.
begin;

-- Freeze corpus DDL/DML for the whole catalog check + atomic cutover. This
-- transaction never acquires G or C, so it cannot wait on advisory authority
-- while holding these relation locks. In-flight writes finish before cutover.
lock table public.content_calendar, public.media_asset, public.media_source,
 public.fixer_forward_media_claim_receipt_20261006,
 public.fixer_forward_media_photo_state_20261007,
 public.fixer_forward_media_photo_key_revocation_20261007,
 public.fixer_owner_photo_revocation_20261007 in access exclusive mode;

do $guard$
declare f record; v_proc record; t text;
begin
 if current_user is distinct from 'postgres' then
  raise exception 'atomic corpus cutover requires postgres' using errcode='23514'; end if;
 for f in select * from (values
  ('portal_action_receipt_apply','p_gym_id text, p_action_id text, p_request_fingerprint text, p_prepared jsonb','3e17e0f6392bbc4ba4dd8ea72ff718f8',true),
  ('stage_lasso_campaign_row','p_row jsonb, p_artifact_tenant text, p_source_hash text, p_policy_version text, p_image_sha256 text','9054fad04a968a88a5cfb4db8a23347b',true),
  ('fixer_forward_calendar_entry_lock_20261008','','f7804f90164613bff2e70d7f7bc5b4e2',true),
  ('fixer_owner_photo_corpus_write_lock_20261007','','12af6fc87e424ab5a5041a4c6d8765d5',true)
 ) as expected(name,identity_args,body_md5,security_definer) loop
  if (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace
      where n.nspname='public' and p.proname=f.name) <> 1 then
   raise exception 'atomic corpus cutover function count drift: %',f.name using errcode='23514'; end if;
  select p.*,pg_get_function_identity_arguments(p.oid) as identity_args
   into v_proc from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   where n.nspname='public' and p.proname=f.name;
  if v_proc.identity_args is distinct from f.identity_args
   or md5(v_proc.prosrc) is distinct from f.body_md5
   or pg_get_userbyid(v_proc.proowner) is distinct from 'postgres'
   or v_proc.prosecdef is distinct from f.security_definer or v_proc.provolatile <> 'v' then
   raise exception 'atomic corpus cutover definition drift: %',f.name using errcode='23514'; end if;
 end loop;
 -- #345 is stacked later; if already present, only its exact reviewed body
 -- may participate. No conditional deletion or bypass of a drifted guard.
 if exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace
           where n.nspname='public' and p.proname='fixer_generated_inventory_lock_20261007') then
  if (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace
      where n.nspname='public' and p.proname='fixer_generated_inventory_lock_20261007') <> 1
   or not exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace
    where n.nspname='public' and p.proname='fixer_generated_inventory_lock_20261007'
     and pg_get_function_identity_arguments(p.oid)=''
     and md5(p.prosrc)='8d40842a1c5211262f24b5035e9f4827'
     and pg_get_userbyid(p.proowner)='postgres' and not p.prosecdef) then
   raise exception 'atomic corpus cutover generated inventory drift' using errcode='23514'; end if;
 end if;
 if exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace
  where n.nspname='public' and p.proname not in ('portal_action_receipt_apply','stage_lasso_campaign_row')
   and p.prosrc ~* 'lock[[:space:]]+table[[:space:]]+(public[.])?content_calendar[[:space:]]+in[[:space:]]+share[[:space:]]+row[[:space:]]+exclusive') then
  raise exception 'atomic corpus cutover additional calendar SRE function requires review' using errcode='23514'; end if;
 foreach t in array array['content_calendar','media_asset','media_source',
  'fixer_forward_media_claim_receipt_20261006','fixer_forward_media_photo_state_20261007',
  'fixer_forward_media_photo_key_revocation_20261007','fixer_owner_photo_revocation_20261007'] loop
  if not exists(select 1 from pg_trigger g
    where g.tgrelid=('public.'||t)::regclass and g.tgname='owner_photo_corpus_write'
     and not g.tgisinternal and g.tgtype=62 and g.tgenabled='O'
     and g.tgfoid='public.fixer_owner_photo_corpus_write_lock_20261007()'::regprocedure) then
   raise exception 'atomic corpus cutover owner-photo trigger drift on %',t using errcode='23514'; end if;
  if t in ('media_asset','media_source')
   and to_regprocedure('public.fixer_generated_inventory_lock_20261007()') is not null
   and not exists(select 1 from pg_trigger g
    where g.tgrelid=('public.'||t)::regclass and g.tgname='generated_inventory_lock'
     and not g.tgisinternal and g.tgtype=62 and g.tgenabled='O'
     and g.tgfoid=to_regprocedure('public.fixer_generated_inventory_lock_20261007()')) then
   raise exception 'atomic corpus cutover generated trigger drift on %',t using errcode='23514'; end if;
  if exists(select 1 from pg_trigger g where g.tgrelid=('public.'||t)::regclass
   and not g.tgisinternal and (g.tgtype & 1)=0 and (g.tgtype & 2)=2
   and (g.tgtype & 28)<>0 and g.tgname collate "C" <= '000_fixer_forward_corpus_entry_20261008' collate "C") then
   raise exception 'atomic corpus cutover earlier statement trigger on %',t using errcode='23514'; end if;
 end loop;
end $guard$;

create function public.fixer_forward_corpus_entry_20261008()
returns trigger language plpgsql security definer set search_path=pg_catalog,public
as $$
declare v_graph bigint := hashtextextended('fixer_forward_graph_20261006',0);
begin
 if current_setting('transaction_isolation') <> 'read committed' then
  raise exception 'forward corpus entry requires read committed' using errcode='25000'; end if;
 if tg_table_name in ('fixer_forward_media_photo_state_20261007',
  'fixer_forward_media_photo_key_revocation_20261007','fixer_owner_photo_revocation_20261007') then
  -- An administrator must enter with its final exclusive mode. Refuse a
  -- reused ordinary transaction instead of upgrading its shared graph lock.
  if exists(select 1 from pg_locks l where l.pid=pg_backend_pid() and l.granted
    and l.locktype='advisory' and l.objsubid=1
    and l.classid::bigint=((v_graph>>32)&4294967295)
    and l.objid::bigint=(v_graph&4294967295) and l.mode='ShareLock')
   and not exists(select 1 from pg_locks l where l.pid=pg_backend_pid() and l.granted
    and l.locktype='advisory' and l.objsubid=1
    and l.classid::bigint=((v_graph>>32)&4294967295)
    and l.objid::bigint=(v_graph&4294967295) and l.mode='ExclusiveLock') then
   raise exception 'negative authority requires final exclusive graph entry before ordinary corpus writes'
    using errcode='25000'; end if;
  perform pg_advisory_xact_lock(v_graph);
 else
  perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
 end if;
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 return null;
end; $$;
revoke all on function public.fixer_forward_corpus_entry_20261008()
 from public,anon,authenticated,service_role;

do $install$
declare t text;
begin
 foreach t in array array['content_calendar','media_asset','media_source',
  'fixer_forward_media_claim_receipt_20261006','fixer_forward_media_photo_state_20261007',
  'fixer_forward_media_photo_key_revocation_20261007','fixer_owner_photo_revocation_20261007'] loop
  execute format('create trigger "000_fixer_forward_corpus_entry_20261008" before insert or update or delete on public.%I for each statement execute function public.fixer_forward_corpus_entry_20261008()',t);
 end loop;
end $install$;

-- Rewrite the catalog-frozen definitions without reimplementing their bodies.
-- Pin post-rewrite prosrc as well: no removal from comments, extra statements,
-- weakened business checks, signature changes or hidden helper are accepted.
do $remove_only_two_locks$
declare f record; v_oid oid; v_before text; v_definition text; v_clause text;
begin
 for f in select * from (values
  ('portal_action_receipt_apply','  LOCK TABLE public.content_calendar IN SHARE ROW EXCLUSIVE MODE;', '653db99604cb8624b60b563bde0f242f'),
  ('stage_lasso_campaign_row','  lock table public.content_calendar in share row exclusive mode;', '33e8151ec24b71c8859ddfb356c175e4')
 ) as expected(name,lock_line,after_md5) loop
  select p.oid,p.prosrc,pg_get_functiondef(p.oid) into v_oid,v_before,v_definition
   from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   where n.nspname='public' and p.proname=f.name;
  v_clause := f.lock_line||chr(10);
  if (length(v_before)-length(replace(v_before,v_clause,'')))/length(v_clause) <> 1 then
   raise exception 'atomic corpus cutover expected exactly one SRE clause in %',f.name using errcode='23514'; end if;
  execute replace(v_definition,v_clause,'');
  if (select md5(prosrc) from pg_proc where oid=v_oid) is distinct from f.after_md5 then
   raise exception 'atomic corpus cutover unexpected rewrite of %',f.name using errcode='23514'; end if;
 end loop;
end $remove_only_two_locks$;

revoke truncate on public.content_calendar, public.media_asset, public.media_source,
 public.fixer_forward_media_claim_receipt_20261006,
 public.fixer_forward_media_photo_state_20261007,
 public.fixer_forward_media_photo_key_revocation_20261007,
 public.fixer_owner_photo_revocation_20261007 from public,anon,authenticated,service_role;

do $privileges$
declare t text; r text;
begin
 foreach t in array array['content_calendar','media_asset','media_source',
  'fixer_forward_media_claim_receipt_20261006','fixer_forward_media_photo_state_20261007',
  'fixer_forward_media_photo_key_revocation_20261007','fixer_owner_photo_revocation_20261007'] loop
  foreach r in array array['anon','authenticated','service_role'] loop
   if has_table_privilege(r,'public.'||t,'TRUNCATE') then
    raise exception 'atomic corpus cutover inherited TRUNCATE remains on % for %',t,r using errcode='23514'; end if;
  end loop;
 end loop;
end $privileges$;
commit;
